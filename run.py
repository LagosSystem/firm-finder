import os
import sys
import asyncio
import uvicorn

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

if __name__ == "__main__":
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "8000"))

    print("=" * 60)
    print("  Firm Finder")
    print("  Law firm and contact directory builder (prototype)")
    print("=" * 60)
    print(f"  Web Application: http://{host}:{port}")
    print("  Local Storage: leads.db (SQLite)")
    print("  Press Ctrl+C to terminate.")
    print("=" * 60)

    uvicorn.run(
        "backend.main:app",
        host=host,
        port=port,
        reload=False,
        log_level="info"
    )
