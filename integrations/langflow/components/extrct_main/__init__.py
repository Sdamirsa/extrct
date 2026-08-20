"""extrct_main - the extraction pipeline: Prep - Schema Builder, Prep - Text Wrapper,
Client - Model Server (Ollama + OpenRouter, one node), Run - Structured Extract,
Run - Sweep Extract, Run - Chunk Extract, PostPrep - Extraction Merger.

Folder name IS the sidebar bundle name (spaces fine: files load individually, never as
a package - so no imports here; tests import components by file path)."""
