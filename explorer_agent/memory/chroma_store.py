
"""
ChromaDB implementation of MemoryStore. This is the ONLY file that
imports chromadb. To swap backends later (Qdrant/Weaviate/FAISS), write
a sibling file (e.g. qdrant_store.py) implementing the same MemoryStore
interface, then change ONE line in memory/__init__.py's factory function.

Embeddings: all-MiniLM-L6-v2 as ONNX (Chroma's own export), run locally via
onnxruntime + tokenizers - free, no API calls, and no Hugging Face access.
Model files are located via `_resolve_model_dir()` (config.yaml's
memory.embedding section).
"""

from pathlib import Path
from typing import List, Dict, Any, Optional

import chromadb
from chromadb.utils.embedding_functions.onnx_mini_lm_l6_v2 import ONNXMiniLM_L6_V2

from .base import MemoryStore
from ..config import Config
from ..logging_config import get_logger

logger = get_logger("chroma_store")

EMBEDDING_MODEL_ID = "onnx:all-MiniLM-L6-v2"  # stored in collection metadata to detect model changes
_REQUIRED_MODEL_FILES = ("model.onnx", "tokenizer.json")


def _sanitize_metadata(metadata: Dict[str, Any]) -> Dict[str, Any]:
    """Chroma only accepts str/int/float/bool metadata values - coerce anything else."""
    clean = {}
    for k, v in metadata.items():
        if v is None:
            clean[k] = ""
        elif isinstance(v, (str, int, float, bool)):
            clean[k] = v
        else:
            clean[k] = str(v)
    return clean


def _has_model_files(folder: Path) -> bool:
    return all((folder / f).is_file() for f in _REQUIRED_MODEL_FILES)


def _find_model_folder(root: Path) -> Optional[Path]:
    """The folder under `root` (inclusive) holding model.onnx + tokenizer.json."""
    if _has_model_files(root):
        return root
    for model_file in sorted(root.rglob("model.onnx")):
        if _has_model_files(model_file.parent):
            return model_file.parent
    return None


def _resolve_model_dir() -> Optional[Path]:
    """Folder containing the ONNX model, or None to use Chroma's default cache
    (which Chroma downloads from its S3 bucket - never Hugging Face - if missing)."""
    if Config.EMBEDDING_MODEL_DIR:
        folder = _find_model_folder(Path(Config.EMBEDDING_MODEL_DIR))
        if folder is None:
            raise FileNotFoundError(
                f"memory.embedding.model_dir={Config.EMBEDDING_MODEL_DIR!r} does not contain "
                f"{' and '.join(_REQUIRED_MODEL_FILES)}"
            )
        return folder

    if Config.EMBEDDING_KAGGLE_HANDLE:
        import kagglehub  # only needed for this option

        download = {"dataset": kagglehub.dataset_download, "model": kagglehub.model_download}.get(
            Config.EMBEDDING_KAGGLE_TYPE)
        if download is None:
            raise ValueError(f"memory.embedding.kaggle_type must be 'dataset' or 'model', "
                             f"got {Config.EMBEDDING_KAGGLE_TYPE!r}")
        logger.info("Fetching embedding model from Kaggle %s %s (cached after first download)",
                    Config.EMBEDDING_KAGGLE_TYPE, Config.EMBEDDING_KAGGLE_HANDLE)
        folder = _find_model_folder(Path(download(Config.EMBEDDING_KAGGLE_HANDLE)))
        if folder is None:
            raise FileNotFoundError(
                f"Kaggle {Config.EMBEDDING_KAGGLE_TYPE} {Config.EMBEDDING_KAGGLE_HANDLE!r} contains no folder "
                f"with {' and '.join(_REQUIRED_MODEL_FILES)}"
            )
        return folder

    default_folder = ONNXMiniLM_L6_V2.DOWNLOAD_PATH / ONNXMiniLM_L6_V2.EXTRACTED_FOLDER_NAME
    if not _has_model_files(default_folder) and not Config.EMBEDDING_ALLOW_DOWNLOAD:
        raise FileNotFoundError(
            f"Embedding model not found in {default_folder} and memory.embedding.allow_download is false. "
            "Set memory.embedding.model_dir or memory.embedding.kaggle_handle in config.yaml."
        )
    return None


class LocalMiniLMEmbedding(ONNXMiniLM_L6_V2):
    """Chroma's ONNX all-MiniLM-L6-v2 (tokenization + mean pooling + L2 norm),
    optionally loaded from a custom folder instead of Chroma's download cache."""

    def __init__(self, model_dir: Optional[Path] = None) -> None:
        super().__init__()
        self._model_dir = model_dir
        if model_dir is not None:
            # Parent class reads DOWNLOAD_PATH / EXTRACTED_FOLDER_NAME / <file>.
            self.DOWNLOAD_PATH = model_dir.parent
            self.EXTRACTED_FOLDER_NAME = model_dir.name

    def _download_model_if_not_exists(self) -> None:
        if self._model_dir is None:  # Chroma's cache: download from Chroma's S3 if missing
            super()._download_model_if_not_exists()


class ChromaMemoryStore(MemoryStore):
    def __init__(self, persist_dir: Optional[str] = None, collection_name: Optional[str] = None):
        persist_path = Path(persist_dir or Config.MEMORY_BASE_DIR) / "chroma"
        persist_path.mkdir(parents=True, exist_ok=True)

        self.embedding_model = LocalMiniLMEmbedding(_resolve_model_dir())
        self.client = chromadb.PersistentClient(path=str(persist_path))
        self._collection_name = collection_name or Config.CHROMA_COLLECTION_NAME

        # Vectors from different embedding models are incompatible (different
        # dimensions/spaces). If the collection was built with another model,
        # drop it; memory/__init__.py rebuilds it from the procedural registry.
        self.needs_reindex = False
        existing = {c.name if hasattr(c, "name") else c for c in self.client.list_collections()}
        if self._collection_name in existing:
            collection = self.client.get_collection(self._collection_name)
            built_with = (collection.metadata or {}).get("embedding_model")
            if built_with != EMBEDDING_MODEL_ID:
                logger.warning("Collection %r was built with embedding model %r; recreating it for %s",
                               self._collection_name, built_with, EMBEDDING_MODEL_ID)
                self.needs_reindex = collection.count() > 0
                self.client.delete_collection(self._collection_name)

        # Embeddings are always passed explicitly, so no embedding_function is
        # attached to the collection (avoids Chroma's persisted-EF config checks).
        self.collection = self.client.get_or_create_collection(
            name=self._collection_name, metadata={"embedding_model": EMBEDDING_MODEL_ID},
        )

    def _embed(self, text: str) -> List[float]:
        return self.embedding_model([text])[0].tolist()

    def add(self, id: str, text: str, metadata: Dict[str, Any]) -> None:
        self.collection.upsert(
            ids=[id],
            documents=[text],
            embeddings=[self._embed(text)],
            metadatas=[_sanitize_metadata(metadata)]
        )

    def search(self, query: str, top_k: int = 3,
               filter: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
        if self.collection.count() == 0:
            return []

        results = self.collection.query(
            query_embeddings=[self._embed(query)],
            n_results=top_k,
            where=filter
        )

        ids = results.get("ids", [[]])[0]
        docs = results.get("documents", [[]])[0]
        metas = results.get("metadatas", [[]])[0]
        dists = (results.get("distances", [[]]) or [[None] * len(ids)])[0]

        return [
            {
                "id": ids[i],
                "text": docs[i],
                "metadata": metas[i],
                "distance": dists[i]
            }
            for i in range(len(ids))
        ]

    def delete(self, id: str) -> None:
        self.collection.delete(ids=[id])

    def count(self) -> int:
        return self.collection.count()
