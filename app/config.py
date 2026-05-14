import os

from dotenv import load_dotenv

load_dotenv()

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
PCLOUD_CODE = os.getenv("PCLOUD_CODE", "kZJYSGZ8TaSQch9Ivb9ov25SMaKfmHODDvy")
PCLOUD_BASE_URL = os.getenv("PCLOUD_BASE_URL", "https://eapi.pcloud.com")
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://postgres:12345@localhost:5432/test")

JWT_SECRET = os.getenv("JWT_SECRET", "supersecretkey_change_in_prod12345567899878788678")
JWT_ALGO = "HS256"
JWT_EXPIRE_HOURS = 24

ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "admin@123")

LOCAL_PDF_MODE = os.getenv("LOCAL_PDF_MODE", "false").lower() == "true"
LOCAL_PDF_FOLDER = os.getenv("LOCAL_PDF_FOLDER", "temp_pdfs")

API_TITLE = "Order Extractor API"
API_VERSION = "5.0.0"

ERP_BASE_URL = os.getenv("ERP_BASE_URL", "https://e3k.teboag.ch:4433/e3k.Web")
ERP_PASSWORD = os.getenv("ERP_PASSWORD")
ERP_USERNAME = os.getenv("ERP_USERNAME")
