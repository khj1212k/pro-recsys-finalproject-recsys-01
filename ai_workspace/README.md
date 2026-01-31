# AI Workspace - Newsletter Generation Pipeline

A production-ready AI system for automated news aggregation, clustering, and newsletter generation.

## 📁 Project Structure

```
ai_workspace/
├── config/              # Configuration files
│   ├── settings.py      # Application settings
│   └── llm_config.json  # LLM provider configurations
│
├── core/                # Core business logic
│   ├── clusterer.py     # HDBSCAN clustering
│   ├── embedder.py      # BGE-M3 embedding generation
│   ├── llm_client.py    # LLM API client (Naver/OpenAI)
│   ├── reconstructor.py # Newsletter content generation
│   ├── tone_converter.py # Tone style conversion
│   └── user_embedder.py # User preference embedding
│
├── crawler/             # Data collection & processing
│   ├── rss_collector.py        # RSS feed crawler
│   ├── content_extractor.py    # Article content extraction
│   └── embedding_generator.py  # Batch embedding generation
│
├── db/                  # Database layer
│   ├── schema.py        # PostgreSQL schema definitions
│   ├── connection.py    # Database connection management
│   ├── batch_manager.py # Batch job logging
│   └── user_log_queries.py # User interaction queries
│
├── workflow/            # LangGraph workflow
│   ├── graph.py         # Workflow graph definition
│   ├── nodes.py         # Workflow node functions
│   ├── state.py         # State schema
│   └── evaluators.py    # LLM-based evaluators
│
├── dags/                # Airflow DAG definitions
│   └── daily_newsletter_dag.py
│
├── scripts/             # Utility scripts
│   ├── update_user_embeddings.py  # User embedding updater
│   ├── migrate_db_1024.py         # DB migration script
│   ├── start_airflow_daemon.sh    # Airflow scheduler
│   └── start_airflow_webserver.sh # Airflow webserver
│
├── tests/               # Unit tests (pytest)
│   ├── test_clusterer.py
│   ├── test_evaluators.py
│   ├── test_reconstructor.py
│   └── test_workflow.py
│
├── recommend-engine/    # Recommendation system (separate module)
│
├── main.py              # Main pipeline entry point
├── requirements.txt     # Python dependencies
└── README.md            # This file
```

## 🚀 Pipeline Stages

### **Stage 0: User Embedding**
- Generate user preference embeddings from interaction history

### **Stage 1: RSS Collection**
- Collect news articles from configured RSS feeds
- Filter by recency (configurable time window)

### **Stage 2: Content Extraction**
- Extract full article content using Trafilatura
- Validate and clean text data

### **Stage 3: News Embedding**
- Generate BGE-M3 embeddings for articles
- Batch processing with GPU support

### **Stage 4-5: Clustering & Newsletter Generation**
- HDBSCAN clustering to group related articles
- LangGraph workflow for newsletter creation:
  - Cluster evaluation (coherence check)
  - Newsletter draft generation
  - Quality evaluation
  - Tone conversion (optional)
  - Database persistence

### **Stage 6: Newsletter Embedding**
- Batch embedding generation for newsletters
- Used by recommendation system

## 📦 Installation

```bash
# Install dependencies
pip install -r requirements.txt

# Copy and configure environment
cp .env.example .env
# Edit .env with your API keys and database credentials
```

## 🔧 Configuration

### Environment Variables (`.env`)
```bash
# Database
DB_HOST=localhost
DB_PORT=5432
DB_NAME=your_db
DB_USER=your_user
DB_PASSWORD=your_password

# LLM API (Naver HyperCLOVA X or OpenAI)
NAVER_API_KEY=your_naver_key
NAVER_API_KEY_PRIMARY=your_primary_key
NAVER_REQUEST_ID=your_request_id

# Optional: OpenAI
OPENAI_API_KEY=your_openai_key
```

### Settings (`config/settings.py`)
- Embedding batch size
- Clustering parameters
- LLM temperature and token limits

## 🏃 Usage

### Run Full Pipeline
```bash
cd ai_workspace
python main.py
```

### Run with Options
```bash
# Skip DB reset
python main.py --no-reset

# Set minimum newsletter target
python main.py --min-target 10

# Use CPU for embeddings (no GPU)
python main.py --force-cpu

# Set worker count
python main.py --workers 8
```

### Update User Embeddings (Standalone)
```bash
python scripts/update_user_embeddings.py --all
python scripts/update_user_embeddings.py --user-id 123
```

### Run Tests
```bash
pytest tests/
```

## 🔄 Airflow Integration

Start Airflow scheduler and webserver:
```bash
bash scripts/start_airflow_daemon.sh
bash scripts/start_airflow_webserver.sh
```

Access Airflow UI: `http://localhost:8080`

## 📊 Database Schema

See `db/schema.py` for full schema definitions. Key tables:
- `news_raw` - Raw news articles with embeddings
- `news_letter` - Generated newsletters
- `user` - User profiles with preference embeddings
- `user_newsletter_ctr_log` - User interaction logs
- `cluster_history` - Clustering batch logs

## 🧪 Testing

Test files are organized in `tests/`:
- `test_clusterer.py` - Clustering logic
- `test_evaluators.py` - LLM evaluators
- `test_reconstructor.py` - Newsletter generation
- `test_workflow.py` - LangGraph workflow

Run with: `pytest tests/ -v`

## 🔐 Security

- API keys stored in `.env` (not committed to git)
- Database credentials managed via environment variables
- Input validation and sanitization in extractors

## 📝 Notes

- GPU recommended for embedding generation (Tesla V100 or better)
- Rate limiting handled automatically for LLM APIs
- Sequential execution mode prevents API throttling
- Batch embedding in Stage 6 optimizes GPU usage

## 🐛 Troubleshooting

### OOM (Out of Memory)
- Reduce batch size in `config/settings.py`
- Use `--force-cpu` flag
- Ensure GPU cleanup is enabled

### Rate Limiting (429 errors)
- Pipeline uses sequential execution by default
- Exponential backoff implemented
- Adjust semaphore in `main.py` if needed

### Database Connection Issues
- Verify credentials in `.env`
- Check PostgreSQL is running
- Ensure `pgvector` extension is installed

## 📚 Dependencies

See `requirements.txt` for full list. Key dependencies:
- `langgraph` - Workflow orchestration
- `FlagEmbedding` - BGE-M3 embeddings
- `hdbscan` - Clustering
- `psycopg2` - PostgreSQL adapter
- `trafilatura` - Content extraction
- `apache-airflow` - Workflow scheduling

## 📄 License

[Your License Here]

## 👥 Authors

[Your Team/Name Here]
