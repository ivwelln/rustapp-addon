import httpx2

from app.main import app


async def test_dashboard_is_local_and_assets_are_served():
    transport = httpx2.ASGITransport(app=app)
    async with httpx2.AsyncClient(base_url="http://127.0.0.1", transport=transport) as client:
        page = await client.get("/")
        assert page.status_code == 200 and "Модерация" in page.text
        assert page.headers["cache-control"] == "no-store"
        assert "frame-ancestors 'none'" in page.headers["content-security-policy"]
        assert (await client.get("/dashboard-assets/dashboard.js")).status_code == 200
        assert (await client.get("/dashboard-assets/dashboard.css")).status_code == 200
        external = await client.get("/api/dashboard", headers={"Host": "other.example"})
        assert external.status_code == 403
