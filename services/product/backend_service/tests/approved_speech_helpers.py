"""Authoritative in-memory authoring fixture for approved speech tests."""

from backend.application.script_authoring.fingerprints import (
    ApprovalDependencies,
    approval_dependency_hash,
)
from backend.application.script_authoring.models import (
    Approval,
    LiveSessionBrief,
    ScriptItem,
    ScriptSet,
    ScriptState,
    ScriptVersion,
    new_id,
)
from backend.application.script_authoring.runtime_handoff import ResolvedApprovedScript
from backend.application.script_authoring.session_binding import DependencyFingerprint

CLAIM = "Giao hàng trong ba ngày."
TEXT = "Thông tin sản phẩm đã được duyệt.\n" + CLAIM


class Source:
    def __init__(self):
        self.set = ScriptSet(
            id=new_id("script_set"),
            shop_id="shop",
            product_ids=["product-1"],
            brief=LiveSessionBrief(
                tenant_id="tenant-1",
                business_session_id="business-1",
                product_facts_version="facts-v1",
                fact_source="merchant",
                product_facts={
                    "product-1": {
                        "product_name": "Sản phẩm",
                        "prices": ["100000 VND"],
                        "allowed_claims": [CLAIM],
                    }
                },
            ),
        )
        self.item = ScriptItem(
            id=new_id("script_item"),
            script_set_id=self.set.id,
            product_id="product-1",
            state=ScriptState.APPROVED,
        )
        self.version = ScriptVersion(
            id=new_id("script_version"),
            script_item_id=self.item.id,
            version=1,
            spoken_text=TEXT,
        )
        self.item.approved_version_id = self.version.id
        self.item.current_version_id = self.version.id
        self.approval = Approval(
            id=new_id("approval"),
            script_item_id=self.item.id,
            script_version_id=self.version.id,
            actor="trusted-human",
            gate_run_id=new_id("gate_run"),
            approval_hash=approval_dependency_hash(
                ApprovalDependencies(
                    spoken_text=TEXT,
                    product_facts_version="facts-v1",
                    rule_set="rules-v1",
                )
            ),
        )
        self.deps = DependencyFingerprint(rule_set_version="rules-v1")

    async def get_binding_script_set(self, **kwargs):
        return self.set

    async def get_script_set(self, **kwargs):
        return self.set

    async def get_script_item(self, **kwargs):
        return self.item

    async def get_script_version(self, **kwargs):
        return self.version

    async def get_approval(self, **kwargs):
        return self.approval

    async def get_recorded_dependencies(self, **kwargs):
        return {"rule_set_version": "rules-v1", "product_facts_version": "facts-v1"}

    def current_dependencies(self):
        return self.deps

    async def get_approved_version(self, **kwargs):
        return ResolvedApprovedScript("product-1", self.version.id, self.version.spoken_text)


async def authorize_session(container, session_id, text):
    source = Source()
    source.version = source.version.model_copy(
        update={
            "spoken_text": text,
            "text_hash": ScriptVersion.compute_text_hash(text),
        }
    )
    source.set.brief.product_facts["product-1"]["allowed_claims"] = [text]
    source.approval.approval_hash = approval_dependency_hash(
        ApprovalDependencies(
            spoken_text=text,
            product_facts_version="facts-v1",
            rule_set="rules-v1",
        )
    )
    container.script_authoring_service = source
    meta = dict(await container.store.get(session_id))
    meta["execution_contract"] = {
        "tenant_id": "tenant-1",
        "business_session_id": "business-1",
        "runtime_session_id": session_id,
        "generation": "fixture-generation",
    }
    meta["script_set_binding"] = {
        "script_set_id": source.set.id,
        "products": [
            {
                "product_id": "product-1",
                "approved_version_id": source.version.id,
                "spoken_text": text,
            }
        ],
    }
    await container.store.set(session_id, meta)
    return source
