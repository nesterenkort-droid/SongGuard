"""HTTP-layer smoke test against the running web service (real sessions + DB).

Run:  docker compose run --rm -v D:/NG:/src -w /src web python -m scripts.verify_http
Drives the real passwordless login flow over HTTP and fetches an authenticated page.
"""

import asyncio
import re

import httpx

BASE = "http://web:8000"


def ok(cond: bool) -> str:
    return "✅" if cond else "❌"


async def main() -> None:
    async with httpx.AsyncClient(base_url=BASE, follow_redirects=False, timeout=30) as c:
        r = await c.get("/healthz")
        print(f"{ok(r.status_code == 200)} GET /healthz → {r.status_code} ({r.json()['status']})")

        r = await c.get("/")
        loc = r.headers.get("location")
        print(f"{ok(r.status_code == 303 and loc == '/login')} GET / (аноним) → {r.status_code} {loc}")

        r = await c.get("/login")
        print(f"{ok(r.status_code == 200 and 'Вход' in r.text)} GET /login → страница входа")

        # Start login, extract the nonce from the returned fragment.
        r = await c.post("/login/start")
        m = re.search(r"nonce=([A-Za-z0-9_-]+)", r.text)
        nonce = m.group(1)
        print(f"{ok(bool(nonce))} POST /login/start → nonce {nonce[:10]}…")

        # Simulate the bot confirming, as admin tg 42 (exists from verify_m1).
        r = await c.post(
            "/dev/tg-confirm",
            data={"payload": f"login-{nonce}", "tg_user_id": "42", "display_name": "Verify Admin"},
        )
        body = r.json()
        print(f"{ok(r.status_code == 200 and body['ok'])} POST /dev/tg-confirm → {body.get('message')}")

        # Poll: this sets the session cookie on our client and returns HX-Redirect.
        r = await c.get(f"/login/poll?nonce={nonce}")
        hx = r.headers.get("hx-redirect")
        print(f"{ok(r.status_code == 204 and hx == '/')} GET /login/poll → сессия выдана (HX-Redirect {hx})")

        # Authenticated now: dashboard + catalog.
        r = await c.get("/")
        print(f"{ok(r.status_code == 200 and 'Панель' in r.text)} GET / (в сессии) → дашборд")

        r = await c.get("/catalog")
        print(f"{ok(r.status_code == 200 and 'TWXNY' in r.text)} GET /catalog → каталог с TWXNY виден")

        r = await c.get("/admin")
        print(f"{ok(r.status_code == 200 and 'Приглашения' in r.text)} GET /admin → админка (роль admin)")

        # Logout clears the session.
        r = await c.get("/logout")
        r = await c.get("/", follow_redirects=False)
        print(f"{ok(r.status_code == 303)} GET /logout → сессия сброшена")

    print("\n✅ HTTP verification complete.")


if __name__ == "__main__":
    asyncio.run(main())
