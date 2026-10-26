"""Document Intelligence track (plan section 1.3), kept separate from the PDF-layer path.

Nothing in the core pipeline imports this package. It reads the Phase 1 output (elements.json), decides
where DI is needed, optionally calls the service (REST, no SDK needed), and writes its own elements file.
"""
