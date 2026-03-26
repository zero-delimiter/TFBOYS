import argparse
import base64
import json
import sys
import time
from io import BytesIO
from pathlib import Path

import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
from PIL import Image
from torchvision import transforms
from transformers import CLIPTextModel, CLIPTokenizer

ROOT = Path(__file__).parent
VQVAE_CKPT = ROOT / "checkpoints" / "vqvae_best.pt"
TRANSFORMER_CKPT = ROOT / "checkpoints" / "transformer_best.pt"
OUTPUT_DIR = ROOT / "web_output"
STATIC_DIR = ROOT / "web_static"
CLIP_MODEL = "openai/clip-vit-base-patch32"

CATEGORY_TAGS = {
    "block": "minecraft block texture",
    "item": "minecraft item texture, flat sprite",
}
QUALITY_TAGS = (
    "16x16 pixel art, high quality, clean pixels, minecraft style, crisp edges"
)

class ResBlock(nn.Module):
    def __init__(self, ch):
        super().__init__()
        self.net = nn.Sequential(
            nn.Conv2d(ch, ch, 3, padding=1),
            nn.GroupNorm(8, ch),
            nn.SiLU(),
            nn.Conv2d(ch, ch, 3, padding=1),
            nn.GroupNorm(8, ch),
        )
        self.act = nn.SiLU()

    def forward(self, x):
        return self.act(x + self.net(x))

class Encoder(nn.Module):
    def __init__(self, in_ch=4, embed_dim=64):
        super().__init__()
        self.net = nn.Sequential(
            nn.Conv2d(in_ch, 128, 4, stride=2, padding=1),
            nn.SiLU(),
            ResBlock(128),
            nn.Conv2d(128, 256, 4, stride=2, padding=1),
            nn.SiLU(),
            ResBlock(256),
            ResBlock(256),
            nn.Conv2d(256, embed_dim, 1),
        )

    def forward(self, x):
        return self.net(x)


class Decoder(nn.Module):
    def __init__(self, out_ch=4, embed_dim=64):
        super().__init__()
        self.net = nn.Sequential(
            nn.Conv2d(embed_dim, 256, 1),
            ResBlock(256),
            ResBlock(256),
            nn.ConvTranspose2d(256, 128, 4, stride=2, padding=1),
            nn.SiLU(),
            ResBlock(128),
            nn.ConvTranspose2d(128, 64, 4, stride=2, padding=1),
            nn.SiLU(),
            ResBlock(64),
            nn.Conv2d(64, out_ch, 1),
            nn.Sigmoid(),
        )

    def forward(self, x):
        return self.net(x)


class VectorQuantizer(nn.Module):
    def __init__(self, codebook_size, embed_dim, commitment=0.25):
        super().__init__()
        self.codebook_size = codebook_size
        self.embed_dim = embed_dim
        self.commitment = commitment
        self.decay = 0.99
        self.eps = 1e-5
        self.embedding = nn.Embedding(codebook_size, embed_dim)
        nn.init.uniform_(self.embedding.weight, -1 / codebook_size, 1 / codebook_size)
        self.register_buffer("ema_cluster_size", torch.zeros(codebook_size))
        self.register_buffer("ema_embed", self.embedding.weight.data.clone())

    def forward(self, z):
        B, D, H, W = z.shape
        z_flat = z.permute(0, 2, 3, 1).reshape(-1, D)
        dist = (
            z_flat.pow(2).sum(1, keepdim=True)
            - 2 * z_flat @ self.embedding.weight.T
            + self.embedding.weight.pow(2).sum(1)
        )
        idx_flat = dist.argmin(1)
        z_q_flat = self.embedding(idx_flat)
        if self.training:
            one_hot = F.one_hot(idx_flat, self.codebook_size).float()
            cluster_size = one_hot.sum(0)
            embed_sum = one_hot.T @ z_flat.detach()
            self.ema_cluster_size.mul_(self.decay).add_(
                cluster_size, alpha=1 - self.decay
            )
            self.ema_embed.mul_(self.decay).add_(embed_sum, alpha=1 - self.decay)
            n = self.ema_cluster_size.sum()
            smoothed = (
                (self.ema_cluster_size + self.eps)
                / (n + self.codebook_size * self.eps)
                * n
            )
            self.embedding.weight.data.copy_(self.ema_embed / smoothed.unsqueeze(1))
        loss = self.commitment * F.mse_loss(z_flat, z_q_flat.detach())
        z_q_flat = z_flat + (z_q_flat - z_flat).detach()
        z_q = z_q_flat.reshape(B, H, W, D).permute(0, 3, 1, 2)
        indices = idx_flat.reshape(B, H, W)
        return z_q, loss, indices

    @torch.no_grad()
    def codebook_usage(self):
        return (self.ema_cluster_size > 1.0).float().mean().item()


class VQVAE(nn.Module):
    def __init__(self, codebook_size=512, embed_dim=64, commitment=0.25):
        super().__init__()
        self.encoder = Encoder(4, embed_dim)
        self.quantizer = VectorQuantizer(codebook_size, embed_dim, commitment)
        self.decoder = Decoder(4, embed_dim)

    def forward(self, x):
        z = self.encoder(x)
        z_q, vq_loss, indices = self.quantizer(z)
        return self.decoder(z_q), vq_loss, indices

    @torch.no_grad()
    def encode(self, x):
        _, _, indices = self.quantizer(self.encoder(x))
        return indices

    @torch.no_grad()
    def decode_indices(self, indices):
        z_q = self.quantizer.embedding(indices).permute(0, 3, 1, 2)
        return self.decoder(z_q)


class CausalSelfAttention(nn.Module):
    def __init__(self, d_model, n_heads, dropout):
        super().__init__()
        self.n_heads = n_heads
        self.d_head = d_model // n_heads
        self.qkv = nn.Linear(d_model, 3 * d_model, bias=False)
        self.proj = nn.Linear(d_model, d_model, bias=False)
        self.drop = nn.Dropout(dropout)

    def forward(self, x):
        B, T, C = x.shape
        q, k, v = self.qkv(x).split(C, dim=2)
        q = q.view(B, T, self.n_heads, self.d_head).transpose(1, 2)
        k = k.view(B, T, self.n_heads, self.d_head).transpose(1, 2)
        v = v.view(B, T, self.n_heads, self.d_head).transpose(1, 2)
        x = F.scaled_dot_product_attention(
            q, k, v, is_causal=True, dropout_p=self.drop.p if self.training else 0.0
        )
        return self.proj(x.transpose(1, 2).contiguous().view(B, T, C))


class CrossAttention(nn.Module):
    def __init__(self, d_model, n_heads, d_context, dropout):
        super().__init__()
        self.n_heads = n_heads
        self.d_head = d_model // n_heads
        self.q = nn.Linear(d_model, d_model, bias=False)
        self.k = nn.Linear(d_context, d_model, bias=False)
        self.v = nn.Linear(d_context, d_model, bias=False)
        self.proj = nn.Linear(d_model, d_model, bias=False)
        self.drop = nn.Dropout(dropout)

    def forward(self, x, context):
        B, T, C = x.shape
        _, S, _ = context.shape
        q = self.q(x).view(B, T, self.n_heads, self.d_head).transpose(1, 2)
        k = self.k(context).view(B, S, self.n_heads, self.d_head).transpose(1, 2)
        v = self.v(context).view(B, S, self.n_heads, self.d_head).transpose(1, 2)
        x = F.scaled_dot_product_attention(
            q, k, v, dropout_p=self.drop.p if self.training else 0.0
        )
        return self.proj(x.transpose(1, 2).contiguous().view(B, T, C))


class TransformerBlock(nn.Module):
    def __init__(self, d_model, n_heads, d_ff, d_context, dropout):
        super().__init__()
        self.ln1 = nn.LayerNorm(d_model)
        self.ln2 = nn.LayerNorm(d_model)
        self.ln3 = nn.LayerNorm(d_model)
        self.self_attn = CausalSelfAttention(d_model, n_heads, dropout)
        self.cross_attn = CrossAttention(d_model, n_heads, d_context, dropout)
        self.ff = nn.Sequential(
            nn.Linear(d_model, d_ff),
            nn.GELU(),
            nn.Dropout(dropout),
            nn.Linear(d_ff, d_model),
            nn.Dropout(dropout),
        )

    def forward(self, x, context):
        x = x + self.self_attn(self.ln1(x))
        x = x + self.cross_attn(self.ln2(x), context)
        x = torch.nan_to_num(x, nan=0.0)
        x = x + self.ff(self.ln3(x))
        return x


class PixelTransformer(nn.Module):
    def __init__(
        self,
        codebook_size,
        seq_len,
        d_model,
        n_layers,
        n_heads,
        d_ff,
        d_context,
        dropout,
    ):
        super().__init__()
        self.seq_len = seq_len
        self.codebook_size = codebook_size
        self.BOS = codebook_size
        vocab_size = codebook_size + 1
        self.tok_emb = nn.Embedding(vocab_size, d_model)
        self.pos_emb = nn.Embedding(seq_len + 1, d_model)
        self.blocks = nn.ModuleList(
            [
                TransformerBlock(d_model, n_heads, d_ff, d_context, dropout)
                for _ in range(n_layers)
            ]
        )
        self.ln_f = nn.LayerNorm(d_model)
        self.head = nn.Linear(d_model, codebook_size, bias=False)
        for m in self.modules():
            if isinstance(m, nn.Linear):
                nn.init.normal_(m.weight, std=0.02)
                if m.bias is not None:
                    nn.init.zeros_(m.bias)
            elif isinstance(m, nn.Embedding):
                nn.init.normal_(m.weight, std=0.02)

    def forward(self, tokens, context):
        B, T = tokens.shape
        bos = torch.full((B, 1), self.BOS, dtype=torch.long, device=tokens.device)
        inp = torch.cat([bos, tokens], dim=1)
        pos = torch.arange(T + 1, device=tokens.device)
        x = self.tok_emb(inp) + self.pos_emb(pos)
        x = x.float()
        ctx = context.float()
        for block in self.blocks:
            x = block(x, ctx)
        return self.head(self.ln_f(x)[:, :T])

    @torch.no_grad()
    def generate(self, context, temperature=1.0, top_k=0):
        B = context.shape[0]
        seq = torch.empty(B, 0, dtype=torch.long, device=context.device)
        for _ in range(self.seq_len):
            bos = torch.full((B, 1), self.BOS, dtype=torch.long, device=context.device)
            inp = torch.cat([bos, seq], dim=1) if seq.shape[1] > 0 else bos
            pos = torch.arange(inp.shape[1], device=context.device)
            x = self.tok_emb(inp) + self.pos_emb(pos)
            x = x.float()
            ctx = context.float()
            for block in self.blocks:
                x = block(x, ctx)
            logits = self.head(self.ln_f(x)[:, -1]) / temperature
            if top_k > 0:
                v, _ = logits.topk(top_k)
                logits[logits < v[:, -1:]] = float("-inf")
            next_tok = torch.multinomial(logits.softmax(-1), 1)
            seq = torch.cat([seq, next_tok], dim=1)
        return seq


class FrozenCLIPEncoder(nn.Module):
    def __init__(self, model_name=CLIP_MODEL):
        super().__init__()
        self.tokenizer = CLIPTokenizer.from_pretrained(model_name)
        self.model = CLIPTextModel.from_pretrained(model_name).float()
        self.model.eval()
        for p in self.model.parameters():
            p.requires_grad_(False)

    @torch.no_grad()
    def encode(self, prompts, device):
        tokens = self.tokenizer(
            prompts,
            padding="max_length",
            max_length=77,
            truncation=True,
            return_tensors="pt",
        ).to(device)
        out = self.model(**tokens).last_hidden_state.float()
        return torch.nan_to_num(out, nan=0.0, posinf=0.0, neginf=0.0)


def load_all_models(device):
    print(f"Loading VQ-VAE from {VQVAE_CKPT}...")
    vd = torch.load(VQVAE_CKPT, map_location=device, weights_only=False)
    cfg = vd["config"]
    vqvae = VQVAE(cfg["codebook_size"], cfg["embed_dim"], cfg["commitment"]).to(device)
    vqvae.load_state_dict(vd["model"])
    vqvae.eval()

    print(f"Loading Transformer from {TRANSFORMER_CKPT}...")
    td = torch.load(TRANSFORMER_CKPT, map_location=device, weights_only=False)
    tcfg = td["config"]
    transformer = PixelTransformer(
        codebook_size=cfg["codebook_size"],
        seq_len=tcfg["seq_len"],
        d_model=tcfg["d_model"],
        n_layers=tcfg["n_layers"],
        n_heads=tcfg["n_heads"],
        d_ff=tcfg["d_ff"],
        d_context=512,
        dropout=0.0,
    ).to(device)
    transformer.load_state_dict(td["model"])
    transformer.eval()

    print("Loading CLIP...")
    clip = FrozenCLIPEncoder(CLIP_MODEL).to(device)

    print(f"All models loaded on {device}")
    return vqvae, transformer, clip


def build_prompt(base, category):
    cat_tag = CATEGORY_TAGS.get(category.lower(), "minecraft texture")
    return f"{base.strip()}, {cat_tag}, {QUALITY_TAGS}"


@torch.no_grad()
def generate_images(
    prompts, vqvae, transformer, clip, device, temperature=1.0, top_k=64
):
    context = clip.encode(prompts, device)
    tokens = transformer.generate(context, temperature=temperature, top_k=top_k)
    tokens_2d = tokens.reshape(len(prompts), 4, 4)
    imgs_t = vqvae.decode_indices(tokens_2d)
    results = []
    for i in range(len(prompts)):
        arr = (imgs_t[i].clamp(0, 1).permute(1, 2, 0).cpu().numpy() * 255).astype(
            "uint8"
        )
        results.append(Image.fromarray(arr, mode="RGBA"))
    return results


def img_to_b64(img, scale=1):
    if scale > 1:
        img = img.resize((img.width * scale, img.height * scale), Image.NEAREST)
    buf = BytesIO()
    img.save(buf, "PNG")
    return base64.b64encode(buf.getvalue()).decode()


def cli_generate(args):
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    vqvae, transformer, clip = load_all_models(device)

    prompt = build_prompt(args.prompt, args.category)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"Prompt : {prompt}")
    print(f"Generating {args.n} image(s)...")

    images = generate_images(
        [prompt] * args.n,
        vqvae,
        transformer,
        clip,
        device,
        temperature=args.temperature,
        top_k=args.top_k,
    )

    safe = args.prompt[:40].replace(" ", "_").replace(",", "").replace("/", "_")
    for i, img in enumerate(images):
        suffix = f"_{i+1}" if args.n > 1 else ""
        p = out_dir / f"{safe}{suffix}.png"
        img.save(p, "PNG")
        img.resize((256, 256), Image.NEAREST).save(
            out_dir / f"{safe}{suffix}_x16.png", "PNG"
        )
        print(f"  Saved: {p}")


def run_webui(port, host):
    from flask import Flask, jsonify, request, send_from_directory

    OUTPUT_DIR.mkdir(exist_ok=True)
    app = Flask(__name__, static_folder=str(STATIC_DIR))

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    vqvae, transformer, clip = load_all_models(device)

    @app.route("/")
    def index():
        return send_from_directory(str(STATIC_DIR), "index.html")

    @app.route("/api/status")
    def api_status():
        return jsonify(
            {
                "device": str(device),
                "model_loaded": True,
                "output_dir": str(OUTPUT_DIR.resolve()),
            }
        )

    @app.route("/api/generate", methods=["POST"])
    def api_generate():
        data = request.json or {}
        prompt_base = data.get("prompt", "").strip()
        category = data.get("category", "item").lower()
        n = max(1, min(64, int(data.get("n", 4))))
        temperature = float(data.get("temperature", 1.0))
        top_k = int(data.get("top_k", 64))

        if not prompt_base:
            return jsonify({"error": "prompt is required"}), 400

        full_prompt = build_prompt(prompt_base, category)
        try:
            images = generate_images(
                [full_prompt] * n,
                vqvae,
                transformer,
                clip,
                device,
                temperature=temperature,
                top_k=top_k,
            )
        except Exception as e:
            return jsonify({"error": str(e)}), 500

        return jsonify(
            {
                "prompt": full_prompt,
                "category": category,
                "images": [
                    {
                        "id": i,
                        "b64": img_to_b64(img),
                        "preview": img_to_b64(img, scale=16),
                    }
                    for i, img in enumerate(images)
                ],
            }
        )

    @app.route("/api/save", methods=["POST"])
    def api_save():
        data = request.json or {}
        b64 = data.get("b64", "")
        prompt = data.get("prompt", "texture")
        if not b64:
            return jsonify({"error": "no image data"}), 400
        try:
            img = Image.open(BytesIO(base64.b64decode(b64)))
            safe = prompt[:40].replace(" ", "_").replace(",", "").replace("/", "_")
            ts = int(time.time())
            fname = f"{safe}_{ts}.png"
            fpath = OUTPUT_DIR / fname
            img.save(fpath, "PNG")
            return jsonify({"saved": fname, "path": str(fpath.resolve())})
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    url = f"http://{host if host != '0.0.0.0' else 'localhost'}:{port}"
    print(f"WebUI ready → {url}")
    app.run(host=host, port=port, debug=False)


def main():
    parser = argparse.ArgumentParser(
        description="TFBOYS WebUI",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python main.py                          # Start Web UI
  python main.py --port 8080              # Custom port
  python main.py generate "Diamond Ore"   # CLI single
  python main.py generate "Iron Sword" --n 4 --category item
        """,
    )
    parser.add_argument("--port", type=int, default=7860)
    parser.add_argument("--host", type=str, default="0.0.0.0")

    sub = parser.add_subparsers(dest="cmd")
    gen = sub.add_parser("generate", help="Generate from CLI")
    gen.add_argument("prompt")
    gen.add_argument("--n", type=int, default=1)
    gen.add_argument("--category", type=str, default="item", choices=["item", "block"])
    gen.add_argument("--temperature", type=float, default=1.0)
    gen.add_argument("--top_k", type=int, default=64)
    gen.add_argument("--out_dir", type=str, default="output")

    args = parser.parse_args()

    if args.cmd == "generate":
        cli_generate(args)
    else:
        run_webui(args.port, args.host)


if __name__ == "__main__":
    main()
