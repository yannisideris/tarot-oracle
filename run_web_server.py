from __future__ import annotations

import argparse
import os

import uvicorn


def main() -> None:
    parser = argparse.ArgumentParser(description="Tarot LLM Oracle v3 web server")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8000")))
    args = parser.parse_args()
    print(f"Tarot LLM Oracle v3 on http://{args.host}:{args.port}")
    print("Provider cascade: Google AI Studio -> Cloudflare Workers AI -> OpenRouter (Gemma 26B)")
    uvicorn.run("app.server:app", host=args.host, port=args.port, proxy_headers=True, forwarded_allow_ips="*")


if __name__ == "__main__":
    main()
