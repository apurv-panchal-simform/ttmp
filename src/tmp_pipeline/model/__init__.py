"""Model layer (plan prompts X1-X12): request/response types, clients, caching. No concrete live client yet;
handlers depend only on the ``ModelClient`` protocol, so a live Azure OpenAI client slots in later."""
