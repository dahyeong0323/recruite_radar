from app.cli import bootstrap
from app.config import load_settings


if __name__ == "__main__":
    bootstrap(load_settings())
