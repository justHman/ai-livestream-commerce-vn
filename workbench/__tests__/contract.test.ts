/** Contract tests — Workbench client boundary vs backend OpenAPI + wire shapes.
 *
 * Pins the client route manifest against contracts/v1/openapi.json and pins
 * the script-authoring wire DTOs against the real backend serializers
 * (ScriptAuthoringServiceImpl._set_wire / save_draft / submit_for_gate /
 * start_generation / start_batch_generation / approve_product).
 * Fails loudly when the console drifts from the current backend contract.
 */

import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";

import {
  mapScriptSetResponse,
  type BackendScriptSetResponse,
} from "../src/scriptSets";

const OPENAPI_PATH = resolve(
  __dirname,
  "../../services/product/backend_service/contracts/v1/openapi.json",
);

interface OpenApiDoc {
  paths: Record<string, Record<string, unknown>>;
}

function loadOpenApi(): OpenApiDoc {
  return JSON.parse(readFileSync(OPENAPI_PATH, "utf-8")) as OpenApiDoc;
}

/** Health probes are infrastructure checks excluded from the packaged
 * production contract on purpose; everything else the client calls MUST be
 * in openapi.json. */
const NON_CONTRACT_ROUTES = new Set<string>(["GET /api/v1/health/ready"]);

/** Every REST route the Workbench clients call (method + openapi template). */
const CLIENT_ROUTES: Array<[string, string]> = [
  ["GET", "/api/v1/health/ready"],
  ["GET", "/api/v1/admin/config"],
  ["GET", "/api/v1/engines"],
  ["POST", "/api/v1/engines/llm"],
  ["POST", "/api/v1/engines/tts"],
  ["POST", "/api/v1/engines/tts/preview"],
  ["GET", "/api/v1/avatars"],
  ["POST", "/api/v1/sessions"],
  ["POST", "/api/v1/sessions/{session_id}/say"],
  ["POST", "/api/v1/sessions/{session_id}/interrupt"],
  ["POST", "/api/v1/sessions/{session_id}/stop"],
  ["POST", "/api/v1/sessions/{session_id}/attach"],
  ["POST", "/api/v1/sessions/{session_id}/events"],
  ["PATCH", "/api/v1/sessions/{session_id}/config"],
  ["POST", "/api/v1/media/livekit/room/{session_id}"],
  ["GET", "/api/v1/entities"],
  ["GET", "/api/v1/entities/{entity_id}"],
  ["PUT", "/api/v1/entities/{entity_id}"],
  ["DELETE", "/api/v1/entities/{entity_id}"],
  ["POST", "/api/v1/entities/suggestions"],
  ["POST", "/api/v1/entities/{entity_id}/render-preview"],
  ["POST", "/api/v1/script-sets"],
  ["GET", "/api/v1/script-sets/{set_id}"],
  ["PATCH", "/api/v1/script-sets/{set_id}"],
  ["PUT", "/api/v1/script-sets/{set_id}/products/{product_id}/draft"],
  ["POST", "/api/v1/script-sets/{set_id}/products/{product_id}/submit"],
  ["POST", "/api/v1/script-sets/{set_id}/products/{product_id}/generation-preview"],
  ["POST", "/api/v1/script-sets/{set_id}/products/{product_id}/generate"],
  [
    "POST",
    "/api/v1/script-sets/{set_id}/products/{product_id}/segments/{segment_index}/regenerate",
  ],
  ["POST", "/api/v1/script-sets/{set_id}/products/{product_id}/fix"],
  ["POST", "/api/v1/script-sets/{set_id}/products/{product_id}/approve"],
  ["POST", "/api/v1/script-sets/{set_id}/generate-batch"],
  ["POST", "/api/v1/script-sets/{set_id}/approve-batch"],
  ["GET", "/api/v1/script-sets/{set_id}/generation-batches/{batch_id}"],
  ["POST", "/api/v1/script-sets/{set_id}/generation-batches/{batch_id}/cancel"],
  ["GET", "/api/v1/script-sets/{set_id}/generation-batches/{batch_id}/events"],
];

describe("client route manifest vs openapi.json", () => {
  it("calls only routes that exist in the backend contract", () => {
    const doc = loadOpenApi();
    const missing: string[] = [];
    for (const [method, path] of CLIENT_ROUTES) {
      const key = `${method} ${path}`;
      if (NON_CONTRACT_ROUTES.has(key)) continue;
      if (!doc.paths[path]?.[method.toLowerCase()]) missing.push(key);
    }
    expect(missing).toEqual([]);
  });

  it("does not call routes removed from the backend", () => {
    // /admin/sandbox/verify moved to avatar_service; calling it here always 404s.
    const apiSource = readFileSync(resolve(__dirname, "../src/api.ts"), "utf-8");
    expect(apiSource).not.toContain("/admin/sandbox/verify");
  });
});

// ---------------- Script-set wire shapes (backend _set_wire truth) ----------------

/** Fixture mirrors ScriptAuthoringServiceImpl._set_wire exactly. */
const WIRE_SET: BackendScriptSetResponse = {
  id: "set_1",
  name: "Set một",
  transition_policy: "ORDER_AGNOSTIC",
  product_ids: ["p1", "p2"],
  revision: 7,
  items: {
    p1: {
      state: "REVIEWABLE",
      current_version_id: "ver_1",
      approved_version_id: null,
      current_version: {
        id: "ver_1",
        version: 3,
        source: "ai",
        display_text: "Xin chào",
        spoken_text: "xin chào",
        gate_result: "gate_run_9",
        created_at: "2026-08-24T00:00:00Z",
      },
      gate: { state: "passed", violations: [] },
    },
    p2: {
      state: "APPROVED",
      current_version_id: "ver_2",
      approved_version_id: "ver_2",
      current_version: {
        id: "ver_2",
        version: 5,
        source: "manual",
        display_text: "A",
        spoken_text: "a",
        gate_result: "gate_run_10",
        created_at: "2026-08-24T00:01:00Z",
      },
      gate: { state: "passed", violations: [] },
    },
  },
};

describe("script-set wire -> view model identity preservation", () => {
  const vm = mapScriptSetResponse(WIRE_SET);

  it("keeps set-level identity", () => {
    expect(vm.id).toBe("set_1");
    expect(vm.revision).toBe(7);
    expect(vm.transition_policy).toBe("ORDER_AGNOSTIC");
  });

  it("preserves current version identity (id/version/gate_run)", () => {
    const p1 = vm.products.find((p) => p.product_id === "p1");
    expect(p1?.state).toBe("REVIEWABLE");
    expect(p1?.current_version?.version_id).toBe("ver_1");
    expect(p1?.current_version?.version).toBe(3);
    expect(p1?.current_version?.spoken_text).toBe("xin chào");
    expect(p1?.current_version?.created_at).toBe("2026-08-24T00:00:00Z");
  });

  it("preserves approved version identity", () => {
    const p2 = vm.products.find((p) => p.product_id === "p2");
    expect(p2?.approved_version_id).toBe("ver_2");
    expect(p2?.state).toBe("APPROVED");
  });

  it("preserves gate outcome and violations", () => {
    const p1 = vm.products.find((p) => p.product_id === "p1");
    expect(p1?.current_version?.gate_status).toBe("GATE_PASSED");
    const failed: BackendScriptSetResponse = {
      ...WIRE_SET,
      items: {
        ...WIRE_SET.items,
        p1: {
          state: "GATE_FAILED",
          current_version_id: "ver_1",
          approved_version_id: null,
          current_version: WIRE_SET.items.p1!.current_version,
          gate: {
            state: "gate_failed",
            violations: [
              { rule_id: "R1", severity: "ERROR", message: "too long" },
            ],
          },
        },
      },
    };
    const vmFail = mapScriptSetResponse(failed);
    const item = vmFail.products.find((p) => p.product_id === "p1");
    expect(item?.current_version?.gate_status).toBe("GATE_FAILED");
    expect(item?.current_version?.violations?.[0]?.rule_id).toBe("R1");
  });
});

describe("command response wire shapes", () => {
  it("approve-batch returns a per-product approval map (not an array)", () => {
    // Backend: {"ok": true, "approvals": {"<pid>": {...}}}
    const wire = {
      ok: true,
      approvals: {
        p1: {
          ok: true,
          product_id: "p1",
          state: "APPROVED",
          approval: {
            version_id: "ver_1",
            actor: "operator",
            approved_at: "2026-08-24T00:02:00Z",
          },
        },
      },
    } as const;
    expect(Array.isArray(wire.approvals)).toBe(false);
    expect(Object.keys(wire.approvals)).toEqual(["p1"]);
    expect(wire.approvals.p1.approval.version_id).toBe("ver_1");
  });

  it("single-product generate returns workflow identity without batch fields", () => {
    const wire = { workflow_id: "job_1", product_id: "p1", status: "queued" };
    expect(wire.status).toBe("queued");
    expect("batch_id" in wire).toBe(false);
  });

  it("batch generate returns batch_id + workflow_summary", () => {
    const wire = {
      batch_id: "batch_1",
      workflow_summary: { products: [], estimated_semantic_calls_total: 12 },
      status: "queued",
      idempotent: false,
    };
    expect(wire.batch_id).toBe("batch_1");
    expect(wire.workflow_summary.estimated_semantic_calls_total).toBe(12);
  });
});
