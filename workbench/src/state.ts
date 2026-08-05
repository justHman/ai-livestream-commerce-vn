/** Workbench state — single reducer + initial store.
 *
 * All app state lives here; views read from the store and dispatch actions.
 */

import type {
  Draft,
  LifecycleEvent,
  Product,
  ShopProfile,
  TestPreferences,
} from "./api_types";

import { EMPTY_PRODUCT, EMPTY_SHOP } from "./api_types";

export interface SessionState {
  id: string | null;
  mode: string | null;
  livekit: { url: string; token: string } | null;
  attached: boolean;
  lifecycle: LifecycleEvent | null;
}

export interface ResourceState {
  engines: unknown;
  avatars: Array<{ id: string; label: string; ready?: boolean }>;
  llmId: string;
  ttsId: string;
  voiceId: string;
  avatarId: string;
  status: string;
}

export interface VerificationState {
  ready: boolean | null;
  layers: Array<{ name: string; status: string; latency_ms: number }>;
}

export interface AutoDemoState {
  phase: string;
  active: boolean;
}

export interface RootState {
  session: SessionState;
  resources: ResourceState;
  verification: VerificationState;
  draft: Draft;
  autoDemo: AutoDemoState;
  diagnostics: unknown;
  events: Array<{ at: string; message: string; tone: string }>;
}

export type Action =
  | { type: "SESSION_SET"; value: Partial<SessionState> }
  | { type: "SESSION_CLEAR" }
  | { type: "RESOURCES_SET"; value: Partial<ResourceState> }
  | { type: "RESOURCE_SELECT"; field: keyof ResourceState; value: string }
  | { type: "VERIFICATION_SET"; value: VerificationState }
  | { type: "SHOP_FIELD"; field: keyof ShopProfile; value: string }
  | { type: "PRODUCTS_SET"; value: Partial<Draft> }
  | { type: "PRODUCT_ID_CHANGE"; productId: string; nextId: string; jsonText: string }
  | { type: "PRODUCT_FIELD"; productId: string; field: string; value: unknown; jsonText: string }
  | { type: "DRAFT_PATCH"; value: Partial<Draft> }
  | { type: "AUTO_PHASE"; phase: string; active?: boolean }
  | { type: "DIAGNOSTICS_SET"; value: unknown }
  | { type: "LIFECYCLE_EVENT"; value: LifecycleEvent }
  | { type: "EVENT_ADD"; value: { at: string; message: string; tone: string } }
  | { type: "EVENT_CLEAR" };

export function emptyProduct(id: string, name: string): Product {
  return { ...EMPTY_PRODUCT, id, name };
}

export function initialState(): RootState {
  return {
    session: { id: null, mode: null, livekit: null, attached: false, lifecycle: null },
    resources: {
      engines: null,
      avatars: [],
      llmId: "",
      ttsId: "",
      voiceId: "default",
      avatarId: "",
      status: "Chưa discovery.",
    },
    verification: { ready: null, layers: [] },
    draft: {
      shop: { ...EMPTY_SHOP, shop_name: "Shop Nam Beauty", host_name: "Chị Lan", address: "123 Nguyễn Huệ, Q.1, TP.HCM", phone: "0909.123.456", selling_style: "Nhiệt tình, rõ giá, không phóng đại công dụng." },
      products: [],
      selectedProductIds: [],
      productOrder: [],
      jsonText: "[]",
      jsonDirty: false,
      errors: [],
      testPreferences: { initialIngestMode: "batch", commentRate: 0.67 },
    },
    autoDemo: { phase: "idle", active: false },
    diagnostics: null,
    events: [],
  };
}

export function reducer(current: RootState, action: Action): RootState {
  switch (action.type) {
    case "SESSION_SET":
      return { ...current, session: { ...current.session, ...action.value } };
    case "SESSION_CLEAR":
      return { ...current, session: initialState().session, diagnostics: null };
    case "RESOURCES_SET":
      return { ...current, resources: { ...current.resources, ...action.value } };
    case "RESOURCE_SELECT":
      return { ...current, resources: { ...current.resources, [action.field]: action.value } };
    case "VERIFICATION_SET":
      return { ...current, verification: action.value };
    case "SHOP_FIELD":
      return {
        ...current,
        draft: { ...current.draft, shop: { ...current.draft.shop, [action.field]: action.value } },
      };
    case "PRODUCTS_SET":
      return { ...current, draft: { ...current.draft, ...action.value } };
    case "PRODUCT_ID_CHANGE":
      if (action.nextId === action.productId || current.draft.products.some((p) => p.id === action.nextId)) {
        return { ...current, draft: { ...current.draft, errors: [`products.id: trùng ID ${action.nextId}.`] } };
      }
      return {
        ...current,
        draft: {
          ...current.draft,
          products: current.draft.products.map((p) => (p.id === action.productId ? { ...p, id: action.nextId } : p)),
          productOrder: current.draft.productOrder.map((id) => (id === action.productId ? action.nextId : id)),
          selectedProductIds: current.draft.selectedProductIds.map((id) => (id === action.productId ? action.nextId : id)),
          jsonText: action.jsonText,
          jsonDirty: false,
          errors: [],
        },
      };
    case "PRODUCT_FIELD":
      return {
        ...current,
        draft: {
          ...current.draft,
          products: current.draft.products.map((p) =>
            p.id === action.productId ? { ...p, [action.field]: action.value } : p,
          ),
          jsonText: action.jsonText,
          jsonDirty: false,
          errors: [],
        },
      };
    case "DRAFT_PATCH":
      return { ...current, draft: { ...current.draft, ...action.value } };
    case "AUTO_PHASE":
      return {
        ...current,
        autoDemo: { ...current.autoDemo, phase: action.phase, active: action.active ?? current.autoDemo.active },
      };
    case "DIAGNOSTICS_SET":
      return { ...current, diagnostics: action.value };
    case "LIFECYCLE_EVENT":
      return { ...current, session: { ...current.session, lifecycle: action.value } };
    case "EVENT_ADD":
      return { ...current, events: [action.value, ...current.events].filter((_, i) => i < 200) };
    case "EVENT_CLEAR":
      return { ...current, events: [] };
    default:
      return current;
  }
}

export function mergeTestPreferences(draft: Draft, testPreferences: Partial<TestPreferences> | undefined): Draft {
  const base: TestPreferences = draft.testPreferences ?? { initialIngestMode: "batch", commentRate: 0.67 };
  return {
    ...draft,
    testPreferences: {
      initialIngestMode: testPreferences?.initialIngestMode ?? base.initialIngestMode,
      commentRate: testPreferences?.commentRate ?? base.commentRate,
    },
  };
}