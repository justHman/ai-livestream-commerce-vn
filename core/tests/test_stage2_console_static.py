"""Static regression contracts for the Stage 2 operator console."""

from __future__ import annotations

import re
from pathlib import Path


HTML = (Path(__file__).parents[2] / "frontend" / "stage2.html").read_text(encoding="utf-8")


def test_canonical_control_blocks_render_once() -> None:
    block_ids = (
        "sessionPanel",
        "resourcePanel",
        "shopPanel",
        "productsPanel",
        "videoPanel",
        "autoDemoPanel",
        "diagnosticsPanel",
        "eventLogPanel",
    )

    assert all(HTML.count(f'id="{block_id}"') == 1 for block_id in block_ids)


def test_console_uses_one_explicit_state_and_reducer() -> None:
    assert re.search(r"const\s+initialState\s*=\s*\{", HTML)
    assert re.search(r"function\s+reducer\s*\(", HTML)
    assert re.search(r"function\s+dispatch\s*\(", HTML)


def test_auto_demo_ingests_and_displays_one_rolling_batch_of_20_comments() -> None:
    assert "AUTO_DEMO_COMMENT_COUNT = 20" in HTML
    assert "pendingComments" not in HTML
    assert "flushPendingAutoComments" not in HTML
    assert 'while ($("msgFeed").children.length > AUTO_DEMO_COMMENT_COUNT)' in HTML
    assert "await ingestComments(comments, autoDemoAbortController.signal)" in HTML


def test_console_prefills_local_test_tokens_and_local_fixtures() -> None:
    assert 'id="apiToken" type="password" value="local-test-token-' in HTML
    assert 'id="adminToken" type="password" value="local-admin-token-' in HTML
    assert 'id="loadMockProductsBtn"' not in HTML
    assert "LOCAL_PRODUCT_FIXTURES" in HTML
    assert "LOCAL_DRAFT_KEY" in HTML
    assert "LOCAL_DRAFT_VERSION" in HTML
    assert "loadMockProducts()" in HTML


def test_console_retries_protected_auto_loads_after_token_entry() -> None:
    assert '$("adminToken").addEventListener("change", loadProtectedResources)' in HTML
    assert '$("apiToken").addEventListener("change", discoverResources)' in HTML
    assert "async function loadProtectedResources()" in HTML
    assert "if (getAdminToken())" in HTML
    assert "if (getViewerToken())" in HTML


def test_console_has_continuous_demo_controls_and_no_bootstrap_fetch() -> None:
    assert 'id="autoDemoRate"' in HTML
    assert 'min="0.2" max="5"' in HTML
    assert 'id="autoDemoMode"' in HTML
    assert "loadProtectedResources();" not in HTML
    assert "LOCAL_PRODUCT_FIXTURES" in HTML
    assert "localStorage" in HTML
    assert "setTimeout(async () =>" in HTML
    assert "autoDemoGeneration" in HTML
    assert "Auto Demo yêu cầu Start session và Attach cấu hình trước." in HTML
    assert "generation !== autoDemoGeneration" in HTML


def test_console_exposes_runtime_scheduling_controls() -> None:
    controls = ("qaMaxClusters", "qaTimeout", "qaCooldown", "answerVariants", "preparedDepth", "retryCount", "pivotEnter", "pivotExit")
    assert all(f'id="{control}"' in HTML for control in controls)
    assert "applyRuntimeConfigBtn" in HTML
    assert '/api/v1/lite/config' in HTML


def test_manual_speech_is_verbatim_tts_without_llm() -> None:
    assert 'Nội dung nói nguyên văn' in HTML
    assert "generate: false" in HTML


def test_shop_profile_supports_presets_and_custom_values() -> None:
    assert 'id="shopProfileSelect"' in HTML
    assert 'value="custom"' in HTML
    assert "SHOP_PROFILE_PRESETS" in HTML


def test_product_controls_stay_outside_collapsed_details() -> None:
    template = re.search(
        r'<template id="productTemplate">(?P<body>.*?)</template>', HTML, re.DOTALL
    ).group("body")

    assert '<details class="product-card" open>' not in template
    assert '<details class="product-details">' in template
    details = re.search(
        r'<details class="product-details">(?P<body>.*?)</details>', template, re.DOTALL
    ).group("body")
    assert 'data-field="selected"' not in details
    assert 'data-action="move-up"' not in details
    assert 'data-action="move-down"' not in details
    assert ">↑</button>" in template
    assert ">↓</button>" in template


def test_console_declares_all_auto_demo_states() -> None:
    expected = {
        "idle",
        "verifying",
        "attaching",
        "introducing",
        "answering",
        "generating",
        "synthesizing",
        "playback",
        "advancing",
        "stopped",
        "failed",
    }

    assert expected <= set(re.findall(r'"([a-z]+)"', HTML))


def test_console_uses_canonical_diagnostics_without_legacy_aliases() -> None:
    expected = {
        "received_total",
        "buffered_comments",
        "active_comments",
        "director_cycles",
        "active_decision",
        "queued_decisions",
        "completed_speeches",
        "completed_speech_history",
        "singleton_clusters",
        "actionable_clusters",
    }

    assert expected <= set(re.findall(r"\b([a-z_]+)\b", HTML))
    assert "queue.pending" not in HTML
    assert "decisions_emitted" not in HTML


def test_full_diagnostics_have_copyable_scroll_regions() -> None:
    ids = ("selectedCluster", "currentPrompt", "generatedScript", "upcomingWork", "completedHistory")

    assert all(f'id="{item_id}"' in HTML for item_id in ids)
    assert "function clearDiagnosticsView" in HTML
    assert "if (!data) { clearDiagnosticsView();" in HTML
    assert "diagnostic-content" in HTML
    assert "overflow: auto" in HTML
    assert ".slice(0," not in HTML


def test_editable_draft_has_structured_shop_and_ordered_products() -> None:
    fields = ("shopName", "hostName", "shopAddress", "shopPhone", "sellingStyle")

    assert all(f'id="{field}"' in HTML for field in fields)
    assert "selectedProductIds" in HTML
    assert "productOrder" in HTML
    assert 'data-action="move-up"' in HTML
    assert 'data-action="move-down"' in HTML
    assert 'id="productsJson"' in HTML
    assert 'id="configErrors"' in HTML


def test_product_id_edits_update_order_and_selection_references() -> None:
    assert "function replaceProductId" in HTML
    assert "productOrder.map" in HTML
    assert "selectedProductIds.map" in HTML


def test_backend_validation_errors_keep_field_locations() -> None:
    assert "detail.map" in HTML
    assert 'issue.loc.join(".")' in HTML


def test_accessibility_and_tablet_layout_contracts() -> None:
    assert ":focus-visible" in HTML
    assert '@media (max-width: 1024px)' in HTML
    assert 'aria-live="polite"' in HTML
    assert 'aria-label="' in HTML
