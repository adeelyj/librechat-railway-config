from __future__ import annotations

import os

import uvicorn


def main() -> None:
    if os.getenv("SEED_ON_START", "false").lower() == "true":
        from .seed import seed

        result = seed()
        print(f"Bauer Twin database synchronized: {result}")
    port = int(os.getenv("PORT", "8000"))
    uvicorn.run("app:app", host="0.0.0.0", port=port, log_level="info")


if __name__ == "__main__":
    main()

