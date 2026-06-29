"""core.debug — debug mode tools: mock data + traffic simulator.

Lets you test the full pipeline (Director → LLM → TTS → avatar) without real
viewers. The frontend has a Debug panel that starts/stops the simulator.

Public:
  MOCK_PRODUCTS        — 3-product VN e-commerce catalog for testing
  MOCK_SHOP_OWNER      — shop owner persona + system prompt
  MOCK_VIEWER_MSGS     — 200 mock viewer messages (price/QA/buy/chitchat/off-topic/sensitive/spam)
  TrafficSimulator     — background thread feeding mock comments to the Director
"""

from .mock_data import MOCK_PRODUCTS, MOCK_SHOP_OWNER, MOCK_VIEWER_MSGS
from .traffic_sim import TrafficSimulator

__all__ = ["MOCK_PRODUCTS", "MOCK_SHOP_OWNER", "MOCK_VIEWER_MSGS", "TrafficSimulator"]
