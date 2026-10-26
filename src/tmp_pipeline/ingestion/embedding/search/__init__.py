"""Live services for Phase 5, an optional package the core never imports (a test enforces it): Azure OpenAI
embeddings and the Azure AI Search index ``tmp-chunks-v1`` (plan 4.7). Everything goes through the injectable
``Transport``, so tests run against fakes; keys come from the environment and are never logged."""
