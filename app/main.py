from fastapi import FastAPI
from app.api.routes import router

app = FastAPI()

app.include_router(router)

def main():
    print("Hello from ai-subtitle-translator!")


if __name__ == "__main__":
    main()
