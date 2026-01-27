
import os
from dotenv import load_dotenv
from openai import OpenAI

# .env 강제 로드
load_dotenv(".env", override=True)

api_key = os.getenv("OPENAI_API_KEY")
print(f"Loaded API Key (len={len(api_key) if api_key else 0}): {api_key[:10]}...{api_key[-4:] if api_key else ''}")

if not api_key or api_key.startswith("your-"):
    print("❌ API Key is missing or default placeholder.")
    exit(1)

client = OpenAI(api_key=api_key)

try:
    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": "Hello"}],
        max_tokens=5
    )
    print("✅ OpenAI API Test Success!")
    print("Response:", response.choices[0].message.content)
except Exception as e:
    print(f"❌ API Call Failed: {e}")
