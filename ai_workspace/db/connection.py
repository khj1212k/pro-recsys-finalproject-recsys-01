"""
Database connection management for ai_workspace
Independent from the news/ project
"""
import psycopg2
import os
from dotenv import load_dotenv

# Load .env file
load_dotenv()


def get_connection():
    """
    Get database connection using environment variables.
    Falls back to default local development settings if not set.
    """
    config = {
        "host": os.getenv("DB_HOST", "localhost"),
        "port": os.getenv("DB_PORT", "5432"),
        "user": os.getenv("DB_USER", "postgres"),
        "password": os.getenv("DB_PASSWORD", "password"),
        "dbname": os.getenv("DB_NAME", "final_db"),
        "options": "-c client_encoding=UTF8"
    }
    
    try:
        conn = psycopg2.connect(**config)
        return conn
    except Exception as e:
        print(f"DB 연결 실패: {e}")
        raise

