"""SmartFoodOps AI Service — semantic retrieval for the GenAI layer (Week 4, D59).

Two processes share this package and one image: the FastAPI app (`ai.main`), which serves
search and RAG-context requests, and the ingestion worker (`ai.ingestion`), which keeps the
vector database in step with the menus. Both import `common`, so this file stays a docstring —
the same rule every service package follows.
"""
