from app.main import app as app


def main() -> None:
    """Run the development server."""
    import uvicorn

    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=True)


if __name__ == "__main__":
    main()
