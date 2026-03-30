"""Case persistence layer.

Provides an abstract CaseStore interface and a JSON-file implementation.
Swap this out for a database-backed store (Supabase, Postgres, etc.)
when ready for production.
"""

from __future__ import annotations

import json
import os
from abc import ABC, abstractmethod
from dataclasses import asdict
from datetime import date, datetime
from pathlib import Path
from typing import Optional

from trustate.workflow.models import (
    Asset,
    AssetType,
    Beneficiary,
    CaseStage,
    ContactInfo,
    CourtInfo,
    Decedent,
    Document,
    DocumentStatus,
    DocumentType,
    PetitionerInfo,
    ProbateCase,
    ProbateType,
    RelationshipToDecedent,
)


class CaseStore(ABC):
    """Abstract interface for persisting probate cases."""

    @abstractmethod
    def save(self, case: ProbateCase) -> None: ...

    @abstractmethod
    def get(self, case_id: str) -> Optional[ProbateCase]: ...

    @abstractmethod
    def list_by_stage(self, stage: CaseStage) -> list[ProbateCase]: ...

    @abstractmethod
    def list_all(self) -> list[ProbateCase]: ...

    @abstractmethod
    def delete(self, case_id: str) -> None: ...


class _JSONEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, (date, datetime)):
            return obj.isoformat()
        if hasattr(obj, "value"):  # Enums
            return obj.value
        return super().default(obj)


class JSONFileStore(CaseStore):
    """Simple file-based store for development. One JSON file per case."""

    def __init__(self, data_dir: str = "data/cases"):
        self._dir = Path(data_dir)
        self._dir.mkdir(parents=True, exist_ok=True)

    def _path(self, case_id: str) -> Path:
        return self._dir / f"{case_id}.json"

    def save(self, case: ProbateCase) -> None:
        data = asdict(case)
        with open(self._path(case.id), "w") as f:
            json.dump(data, f, cls=_JSONEncoder, indent=2)

    def get(self, case_id: str) -> Optional[ProbateCase]:
        path = self._path(case_id)
        if not path.exists():
            return None
        with open(path) as f:
            data = json.load(f)
        return self._dict_to_case(data)

    def list_by_stage(self, stage: CaseStage) -> list[ProbateCase]:
        return [c for c in self.list_all() if c.stage == stage]

    def list_all(self) -> list[ProbateCase]:
        cases = []
        for path in self._dir.glob("*.json"):
            with open(path) as f:
                data = json.load(f)
            cases.append(self._dict_to_case(data))
        return cases

    def delete(self, case_id: str) -> None:
        path = self._path(case_id)
        if path.exists():
            os.remove(path)

    @staticmethod
    def _dict_to_case(data: dict) -> ProbateCase:
        """Reconstruct a ProbateCase from a dict with full nested object support."""
        if isinstance(data.get("stage"), str):
            data["stage"] = CaseStage(data["stage"])
        if isinstance(data.get("probate_type"), str):
            data["probate_type"] = ProbateType(data["probate_type"])

        # Reconstruct decedent
        if isinstance(data.get("decedent"), dict):
            data["decedent"] = Decedent(**data["decedent"])

        # Reconstruct petitioner
        if isinstance(data.get("petitioner"), dict):
            p = data["petitioner"]
            if isinstance(p.get("contact"), dict):
                p["contact"] = ContactInfo(**p["contact"])
            if isinstance(p.get("relationship"), str):
                p["relationship"] = RelationshipToDecedent(p["relationship"])
            data["petitioner"] = PetitionerInfo(**p)

        # Reconstruct court info
        if isinstance(data.get("court"), dict):
            data["court"] = CourtInfo(**{
                k: v for k, v in data["court"].items()
                if k in CourtInfo.__dataclass_fields__
            })

        # Reconstruct beneficiaries
        bens = []
        for b in data.get("beneficiaries", []):
            if isinstance(b, dict):
                if isinstance(b.get("contact"), dict):
                    b["contact"] = ContactInfo(**b["contact"])
                if isinstance(b.get("relationship"), str):
                    b["relationship"] = RelationshipToDecedent(b["relationship"])
                bens.append(Beneficiary(**b))
            else:
                bens.append(b)
        data["beneficiaries"] = bens

        # Reconstruct assets
        assets = []
        for a in data.get("assets", []):
            if isinstance(a, dict):
                if isinstance(a.get("asset_type"), str):
                    a["asset_type"] = AssetType(a["asset_type"])
                assets.append(Asset(**{
                    k: v for k, v in a.items()
                    if k in Asset.__dataclass_fields__
                }))
            else:
                assets.append(a)
        data["assets"] = assets

        # Reconstruct documents
        docs = []
        for d in data.get("documents", []):
            if isinstance(d, dict):
                if isinstance(d.get("doc_type"), str):
                    d["doc_type"] = DocumentType(d["doc_type"])
                if isinstance(d.get("status"), str):
                    d["status"] = DocumentStatus(d["status"])
                docs.append(Document(**{
                    k: v for k, v in d.items()
                    if k in Document.__dataclass_fields__
                }))
            else:
                docs.append(d)
        data["documents"] = docs

        return ProbateCase(**{
            k: v for k, v in data.items()
            if k in ProbateCase.__dataclass_fields__
        })
