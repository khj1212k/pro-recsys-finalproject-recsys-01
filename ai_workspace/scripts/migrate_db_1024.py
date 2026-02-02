import os
import sys

# Add workspace to path
sys.path.append(os.path.join(os.path.dirname(__file__), '..'))

from db.connection import get_connection

def migrate_to_1024():
    print("🚀 Starting Database Schema Migration (768 -> 1024)...")
    
    conn = get_connection()
    cur = conn.cursor()
    
    try:
        # 1. news_raw table
        print("1. Migrating 'news_raw' table...")
        # Check current dimension (optional, just force it)
        # We drop the index first as it depends on the column type
        cur.execute("DROP INDEX IF EXISTS idx_news_raw_embedding;")
        
        # Alter column type. This might fail if data exists and can't be cast.
        # So easiest way is: Set to NULL, then Alter.
        print("   - Clearing old 768 embeddings...")
        cur.execute("UPDATE news_raw SET embedding_result = NULL;")
        
        print("   - Altering column type to vector(1024)...")
        # Direct ALTER TYPE with USING might be complex for vector, 
        # so dropping and adding is safer/cleaner since we just nulled it.
        cur.execute("ALTER TABLE news_raw DROP COLUMN IF EXISTS embedding_result;")
        cur.execute("ALTER TABLE news_raw ADD COLUMN embedding_result vector(1024);")
        
        # Recreate Index
        print("   - Recreating Index...")
        try:
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_news_raw_embedding
                ON news_raw USING ivfflat (embedding_result vector_cosine_ops)
                WITH (lists = 100);
            """)
        except Exception as e:
            print(f"   (Index creation skipped/failed: {e} - OK if not enough data)")

        # 2. news_letter table
        print("2. Migrating 'news_letter' table...")
        cur.execute("DROP INDEX IF EXISTS idx_news_letter_embedding;")
        
        print("   - Clearing old 768 embeddings...")
        cur.execute("UPDATE news_letter SET news_letter_embedding = NULL;")
        
        print("   - Altering column type to vector(1024)...")
        cur.execute("ALTER TABLE news_letter DROP COLUMN IF EXISTS news_letter_embedding;")
        cur.execute("ALTER TABLE news_letter ADD COLUMN news_letter_embedding vector(1024);")
        
        # Recreate Index
        try:
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_news_letter_embedding
                ON news_letter USING ivfflat (news_letter_embedding vector_cosine_ops)
                WITH (lists = 100);
            """)
        except Exception as e:
            print(f"   (Index creation skipped/failed: {e} - OK if not enough data)")

        conn.commit()
        print("✨ Migration Complete Successfully!")
        
    except Exception as e:
        conn.rollback()
        print(f"❌ Migration Failed: {e}")
        raise
    finally:
        conn.close()

if __name__ == "__main__":
    migrate_to_1024()
