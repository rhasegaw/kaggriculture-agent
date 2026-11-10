"""Kaggriculture agent -- closed-loop planner (no replay tape).

Strategy summary
----------------
* Day 0: 4 sheep on the tiles next to the shed (wool + fertilizer are the
  early cash engine), 10 melons on the far NW tiles (dumped on day 10),
  a little wheat.  Animals sit in a compact zone around the shed so the
  daily feed / care / collect / harvest round is short.
* Cows are added from day 4 as cash arrives (8 by ~day 8, fewer if the town
  never opens a milk shop); sheep scale with yarn stores.
* Strawberries are planted early (from day 3, ~36-42 by day 11), fertilized
  on ages 9 and 13 (doubles every scheduled production) and harvested the
  moment fruit is on the plant.  Wheat fills everything else and is replanted
  until day 27; feed is bought when the farm is short.
* Land: NE around day 5-6, SW after the melon money.  A second melon wave is
  planted on day 11 only if the melon market is not glutted.
* Every turn a job list is built from the observed farm state and units
  (farmer + hands) are matched to jobs by urgency tier, priority, distance and
  stickiness; units finish the work on the tile they stand on before moving.
* Market: fertilizer / melon / premium goods are sold as soon as they reach
  the shed (whoever sells first gets the price), wheat beyond the feed
  reserve is sold, and everything is liquidated by step 718.
"""
import math
import os
import sys


def _knob(name, default):
    try:
        return type(default)(os.environ.get(name, default))
    except Exception:
        return default

# ----------------------------------------------------------------------------
# Engine constants (mirrors kaggle_environments/envs/kaggriculture)
# ----------------------------------------------------------------------------
CROPS = {
    "WHEAT":      {"seed": 10, "first": 2, "maxday": 4, "interval": 0, "max_yield": 6, "ongoing": False},
    "CARROT":     {"seed": 20, "first": 2, "maxday": 3, "interval": 0, "max_yield": 4, "ongoing": False},
    "TOMATO":     {"seed": 50, "first": 8, "maxday": 8, "interval": 1, "max_yield": 4, "ongoing": True},
    "STRAWBERRY": {"seed": 100, "first": 10, "maxday": 10, "interval": 2, "max_yield": 4, "ongoing": True},
    "MELON":      {"seed": 80, "first": 10, "maxday": 12, "interval": 0, "max_yield": 6, "ongoing": False},
}
ANIMALS = {
    "GOOSE": {"cost": 300, "structure": "COOP", "first": 4, "interval": 1, "max_held": 4, "product": "EGG"},
    "COW":   {"cost": 400, "structure": "PASTURE", "first": 8, "interval": 2, "max_held": 6, "product": "MILK"},
    "SHEEP": {"cost": 500, "structure": "PASTURE", "first": 6, "interval": 3, "max_held": 6, "product": "WOOL"},
}
PRODUCTS = ["WHEAT", "CARROT", "TOMATO", "STRAWBERRY", "MELON", "EGG", "MILK", "WOOL", "FERTILIZER"]
PREMIUM = ("MILK", "WOOL", "STRAWBERRY", "MELON")
MARKET_I0 = 10000
MARKET_PARAMS = {
    "WHEAT":      {"base": 25, "T": 400, "bf": "sqrt", "bt": 0.80, "af": "log", "at": 0.20},
    "CARROT":     {"base": 35, "T": 450, "bf": "hinge", "bt": 1.00, "af": "sqrt", "at": 0.70},
    "TOMATO":     {"base": 60, "T": 200, "bf": "hinge", "bt": 0.40, "af": "sqrt", "at": 0.60},
    "STRAWBERRY": {"base": 120, "T": 100, "bf": "sqrt", "bt": 0.70, "af": "linear", "at": 1.60},
    "MELON":      {"base": 250, "T": 300, "bf": "log", "bt": 0.20, "af": "sq", "at": 3.60},
    "EGG":        {"base": 50, "T": 332, "bf": "hinge", "bt": 0.40, "af": "log", "at": 0.20},
    "MILK":       {"base": 160, "T": 122, "bf": "sqrt", "bt": 0.60, "af": "linear", "at": 1.60},
    "WOOL":       {"base": 200, "T": 105, "bf": "log", "bt": 0.20, "af": "sq", "at": 3.20},
    "FERTILIZER": {"base": 100, "T": 200, "bf": "linear", "bt": 0.40, "af": "linear", "at": 0.40},
}
SHOPS = {
    "BAKERY": ["EGG", "WHEAT"],
    "PIZZA_SHOP": ["MILK", "TOMATO", "WHEAT"],
    "BRUNCH_SPOT": ["EGG", "WHEAT", "STRAWBERRY"],
    "YARN_STORE": ["WOOL"],
    "ICE_CREAM_SHOP": ["STRAWBERRY", "MILK", "WHEAT"],
    "PET_CAFE": ["CARROT"],
    "SMOOTHIE_SHOP": ["STRAWBERRY", "MILK"],
    "FARMERS_MARKET": ["WHEAT", "CARROT", "TOMATO", "STRAWBERRY"],
}
LAND_ORDER = ["NE", "SW", "SE"]
LAND_PRICES = [1000, 2000, 4000]
BOARD = 10
TPD = 24
LAST_DAY = 29
LAST_STEP = 718          # last step whose actions are executed
SHED_CAP = 100
SHOP_SELL_INT = 4        # town shops buy 1 unit/product every N steps (config townShopSellInterval)
CENTER_SELL_INT = 24     # town centre buys 1 of each product every N steps (config townCenterSellInterval)
SHOP_UNLOCK_INT = 3      # a new shop every N days (config townShopUnlockInterval)
MAX_SHOPS = 8            # engine MAX_SHOP_INSTANCES
HINGE_GAIN = 8.0         # engine HINGE_GAIN
SHED_TILES = [(4, 4), (5, 4), (4, 5), (5, 5)]
CENTER = (4.5, 4.5)


def _shape(func, x, T):
    x = max(0.0, x)
    if func == "linear":
        return x
    if func == "sq":
        return x * x
    if func == "sqrt":
        return math.sqrt(x)
    if func == "log":
        return math.log(1.0 + x)
    if func == "hinge":
        u = x / T
        return u + HINGE_GAIN * max(0.0, u - 1.0) ** 2
    return x


def market_price(item, inv):
    p = MARKET_PARAMS[item]
    base, T = p["base"], p["T"]
    if inv < MARKET_I0:
        amp = p["bt"] * base / _shape(p["bf"], T, T)
        price = base + amp * _shape(p["bf"], MARKET_I0 - inv, T)
    else:
        amp = p["at"] * base / _shape(p["af"], T, T)
        price = base - amp * _shape(p["af"], inv - MARKET_I0, T)
    return max(1, int(round(price)))


def units_above_price(item, inv, floor_price, max_units):
    """How many units can be sold (one at a time) while price >= floor_price."""
    n = 0
    while n < max_units:
        pr = market_price(item, inv)
        if pr < floor_price:
            break
        n += 1
        if pr > 1:
            inv += 1
    return n


def fib_cost(n):
    a, b = 1, 1
    for _ in range(n):
        a, b = b, a + b
    return a


def hire_total(n):
    return sum(fib_cost(i) for i in range(n))


# ----------------------------------------------------------------------------
# geometry
# ----------------------------------------------------------------------------
def dist(a, b):
    return abs(a[0] - b[0]) + abs(a[1] - b[1])


def nearest_shed(p):
    best, bd = SHED_TILES[0], 99
    for s in SHED_TILES:
        d = dist(p, s)
        if d < bd:
            best, bd = s, d
    return best, bd


def center_dist(t):
    return abs(t[0] - CENTER[0]) + abs(t[1] - CENTER[1])


def euclid2(t):
    return (t[0] - CENTER[0]) ** 2 + (t[1] - CENTER[1]) ** 2


def step_toward(p, q):
    dx, dy = q[0] - p[0], q[1] - p[1]
    if dx == 0 and dy == 0:
        return None
    if abs(dx) >= abs(dy):
        return "EAST" if dx > 0 else "WEST"
    return "SOUTH" if dy > 0 else "NORTH"


def quadrant(x, y):
    return ("N" if y < 5 else "S") + ("W" if x < 5 else "E")


# ----------------------------------------------------------------------------
# persistent per-player state
# ----------------------------------------------------------------------------
ANIMAL_ZONE = _knob("KG_ANIMAL_ZONE", 12)
COW_BASE = _knob("KG_COW_BASE", 8)
COW_PER_MILK_SHOP = _knob("KG_COW_PER_MILK", 1)
COW_CAP = _knob("KG_COW_CAP", 12)
COW_LAST_BUY = _knob("KG_COW_LAST_BUY", 15)
# draw-adaptive sizing (v14 candidate; default OFF): decide from the shops
# drawn so far (days 3/6/9/12) how rich the milk / strawberry markets will be
# and size the herd / strawberry field to the expected town drain.
ADAPT = _knob("KG_ADAPT", 0)
ADAPT_MILK_EXP = _knob("KG_ADAPT_MILK_EXP", 4.0)      # expected final milk shops -> milk-rich
ADAPT_STRAW_EXP = _knob("KG_ADAPT_STRAW_EXP", 5.5)    # expected final strawberry shops -> straw-rich
ADAPT_COW_PER_MILK = _knob("KG_ADAPT_COW_PER_MILK", 2)  # cows beyond 12 per milk shop beyond 3
ADAPT_COW_CAP = _knob("KG_ADAPT_COW_CAP", 18)
ADAPT_COW_LAST = _knob("KG_ADAPT_COW_LAST", 18)
ADAPT_STRAW_MILK = _knob("KG_ADAPT_STRAW_MILK", 30)   # strawberry cap when milk-rich (near tiles go to cows)
ADAPT_STRAW_RICH = _knob("KG_ADAPT_STRAW_RICH", 42)   # strawberry cap when straw-rich
ADAPT_STRAW_RICH2 = _knob("KG_ADAPT_STRAW_RICH2", 42) # ... and very rich (>= 6 expected)
ADAPT_FAR = _knob("KG_ADAPT_FAR", 5.0)
ADAPT_MILK_OVER_YARN = _knob("KG_ADAPT_MILK_OVER_YARN", 1)
ADAPT_CARROT_DAY = _knob("KG_ADAPT_CARROT_DAY", 15)     # carrot wave start when >= 2 pet cafes
ADAPT_CARROT_PER_CAFE = _knob("KG_ADAPT_CARROT_PER_CAFE", 16)
ADAPT_CARROT_CAP = _knob("KG_ADAPT_CARROT_CAP", 40)
ADAPT_CARROT_FARM = _knob("KG_ADAPT_CARROT_FARM", 1)   # count farmers markets as carrot outlets
ADAPT_COW_M2 = _knob("KG_ADAPT_COW_M2", 10)
ADAPT_STRAW_POOR = _knob("KG_ADAPT_STRAW_POOR", 12)     # cap when no strawberry shop by day 9 (0 = off)
ADAPT_STRAW_POOR6 = _knob("KG_ADAPT_STRAW_POOR6", 30)   # cap when none by day 6
ADAPT_STRAW_POOR1 = _knob("KG_ADAPT_STRAW_POOR1", 24)
ADAPT_STRAW_YARN = _knob("KG_ADAPT_STRAW_YARN", 30)
# market-aware sizing (meta-drift defence): infer the opponent's sales per product from
# the shared market inventory (delta + known town drain - my sales) and size the
# tomato / carrot fields and the milk herd against the REMAINING hole, not an
# assumed empty market.
MARKET_AWARE = _knob("KG_MARKET_AWARE", 0)
ENGINE_SYNC = _knob("KG_ENGINE_SYNC", 1)     # follow the running engine's constants / configuration (balance patches)
# opponent-aware sizing: the rival's farm is public (tiles / animals / plants), so
# the milk share game can be played against the OBSERVED rival herd instead of an
# assumed meta build.  Cows are decided on days 8-15 and pay only if the joint
# supply of both herds stays under the town drain of the (expected) milk shops.
OPP_AWARE = _knob("KG_OPP", 0)
OPP_COW_UP = _knob("KG_OPP_COW_UP", 0)           # my herd cap when the rival's herd is small (0 = off; falsified: milk market too thin, tetsuya 17->15)
OPP_COW_RIVAL = _knob("KG_OPP_COW_RIVAL", 5)     # rival herd (cows) at/under which milk is under-supplied
OPP_COW_DOWN = _knob("KG_OPP_COW_DOWN", 0)       # 1: cap my herd by the joint-supply room (0 = off)
OPP_COW_FLOOR = _knob("KG_OPP_COW_FLOOR", 8)     # never cap below this
OPP_MILK_PER_COW = _knob("KG_OPP_MILK_PER_COW", 1.2)  # milk per cow-day (1 base + care bonus, 2-day cycle)
OPP_COW_DAY = _knob("KG_OPP_COW_DAY", 8)         # first day the rival herd is read
# strawberry share game (TESTED, OFF): matching a rival's larger field loses --
# the linear-glut market is thin enough that my extra plants depress the price
# more than the share they win (Cournot best response is to hold, not match).
OPP_STRAW_MATCH = _knob("KG_OPP_STRAW", 0)   # falsified on the 08-18 held-out set (tetsuya 17->12, ReCurSiON 24->22): thin Cournot market
OPP_STRAW_DAY = _knob("KG_OPP_STRAW_DAY", 9)
OPP_STRAW_MARGIN = _knob("KG_OPP_STRAW_MARGIN", 4)
OPP_STRAW_CAP = _knob("KG_OPP_STRAW_CAP", 40)
MA_DAYS = _knob("KG_MA_DAYS", 4)                    # trailing days for the opponent's supply rate      # strawberry cap when a yarn store came early (0 = off)    # cap when exactly one by day 9 (0 = off)
ADAPT_MELON2_SKIP = _knob("KG_ADAPT_MELON2_SKIP", 1)     # no melon wave 2 when milk/straw-rich
ADAPT_MELON2_SKIP_M = _knob("KG_ADAPT_MELON2_SKIP_M", 2)  # ... or this many milk shops by day 10
ADAPT_TOMATO_MIN = _knob("KG_ADAPT_TOMATO_MIN", 2)     # tomato shops (pizza / farmers market) needed
ADAPT_TOMATO_PER_SHOP = _knob("KG_ADAPT_TOMATO_PER_SHOP", 8)
ADAPT_TOMATO_MIN_DEF = _knob("KG_ADAPT_TOMATO_MIN_DEF", 200)  # estimated day-24 deficit for the first 8 plants
ADAPT_TOMATO_CAP = _knob("KG_ADAPT_TOMATO_CAP", 32)
ADAPT_TOMATO_DECIDE = _knob("KG_ADAPT_TOMATO_DECIDE", 19) # shops known by this day size the tomato field
ADAPT_TOMATO_LAST = _knob("KG_ADAPT_TOMATO_LAST", 19)  # last planting day (age 8-11 productions by day 27)          # cows once two milk shops are known (0 = off)
ADAPT_DIG = _knob("KG_ADAPT_DIG", 1)                    # dig young wheat on zone tiles when pastures are needed
ADAPT_YARN_EARLY = _knob("KG_ADAPT_YARN_EARLY", 6)      # first yarn store by this day -> big herd
# v17 candidates (live losses 08-19: tetsuya x2 with a yarn store at day 9, Arman x1 with two stores by day 9 ->
# their 8-9 / 14 sheep vs my 6-7 / 10): the sheep boost day, a higher cap with two stores, and pasture room for it
ADAPT_YARN_SHEEP_DAY = _knob("KG_ADAPT_YARN_SHEEP_DAY", -1)   # sheep boost if the first yarn store is by this day (-1 = ADAPT_YARN_EARLY)
ADAPT_SHEEP_CAP_Y3 = _knob("KG_ADAPT_SHEEP_CAP_Y3", 0)       # sheep cap with >= 3 yarn stores (09-07 leaders: 19-20 sheep, 2 cows)
YARN_COW_CAP = _knob("KG_YARN_COW_CAP", 0)                  # >0: no cow purchases beyond this with >= 2 yarn stores and few milk outlets
YARN_COW_CAP_MILK = _knob("KG_YARN_COW_CAP_MILK", 3)        # ... "few" = milk outlets <= this
ADAPT_SHEEP_CAP_Y2 = _knob("KG_ADAPT_SHEEP_CAP_Y2", 0)       # sheep cap with >= 2 yarn stores by ADAPT_Y2_DAY (0 = off)
ADAPT_Y2_DAY = _knob("KG_ADAPT_Y2_DAY", 15)
ADAPT_ZONE_ANY = _knob("KG_ADAPT_ZONE_ANY", 0)               # 1: pasture room (ADAPT_FAR) whenever the herd target exceeds the zone; 2: only with two yarn stores
# v17-2 feed logistics (72 v16 live games: 1.8% of animal-days unfed, ~12 product units/game lost to uncared /
# unvisited animals, concentrated on the expansion days 12-16): the feed-wheat order counted only PLACED animals
# (not the ones bought this morning / waiting in the shed) and a route stop was abandoned for the day when the
# shed ran dry even though the market phase of that very step buys more.
FEED_WHEAT_ALL = _knob("KG_FEED_WHEAT_ALL", 0)     # 1: feed wheat for unplaced + today's bought animals too (+ FEED_BUFFER)
FEED_BUFFER = _knob("KG_FEED_BUFFER", 2)
ROUTE_WAIT_WHEAT = _knob("KG_ROUTE_WAIT_WHEAT", 0)  # 1: when the shed is dry but wheat is affordable, wait for the buy instead of dropping the stop
CARE_LAST_DAY = _knob("KG_CARE_LAST_DAY", 0)          # 1: care on day 28 for animals producing at the end of day 28
ROUTE_COLLECT_RANK = _knob("KG_ROUTE_COLLECT_RANK", 0)  # 1: the post-feed COLLECT_FERTILIZER keeps the CARE rank too (islet collects 365 vs my 296-347 units)
ROUTE_CARE_RANK = _knob("KG_ROUTE_CARE_RANK", 0)    # >0: A-rank of an animal stop's post-feed CARE/HARVEST when pruning (85 lost close strawberry games; 0 = off)
ADAPT_SHEEP_PER_YARN = _knob("KG_ADAPT_SHEEP_PER_YARN", 7)  # milk-rich cows also under the yarn shift                # pasture distance limit while the herd exceeds the zone
COW_MIN_PRICE = _knob("KG_COW_MIN_PRICE", 200)
YARN_SHIFT = _knob("KG_YARN_SHIFT", 1)          # yarn store in the first draws -> cap cows, more sheep
YARN_COW_CAP = _knob("KG_YARN_COW_CAP", 5)
YARN_SHIFT_MIN = _knob("KG_YARN_SHIFT_MIN", 1)
YARN_COW_FLOOR = _knob("KG_YARN_COW_FLOOR", 0)
YARN_COW_FLOOR_DAY = _knob("KG_YARN_COW_FLOOR_DAY", 12)  # ... once no second yarn store showed by this day      # under the yarn shift keep COW_BASE cows when a milk shop exists       # yarn stores needed before the shift (cow cap) applies
YARN_COW_PER_MILK = _knob("KG_YARN_COW_PER_MILK", 1)
SHEEP_CAP_YARN = _knob("KG_SHEEP_CAP_YARN", 10)      # sheep cap under yarn shift (top agents run 10-12)
SHEEP_PER_YARN_SHIFT = _knob("KG_SHEEP_PER_YARN_SHIFT", 4)  # sheep per yarn store under yarn shift
ANIMAL_FAR_YARN = _knob("KG_ANIMAL_FAR_YARN", 4.0)   # pasture distance limit under yarn shift
LAND3_YARN = _knob("KG_LAND3_YARN", 0)               # buy the 4th quadrant (SE, $4000) under yarn shift
LAND3_DAY = _knob("KG_LAND3_DAY", 10)
LAND1_DAY = _knob("KG_LAND1_DAY", 4)
EARLY_RESERVE_DAYS = _knob("KG_EARLY_RESERVE_DAYS", 3)  # feed reserve (days) before day 8                # earliest day for the 2nd quadrant
ANIMAL_FAR_LIMIT = _knob("KG_ANIMAL_FAR", 4.0)
SHEEP_CAP = _knob("KG_SHEEP_CAP", 10)
SHEEP_PER_YARN = _knob("KG_SHEEP_PER_YARN", 4)
SHEEP_LAST_BUY = _knob("KG_SHEEP_LAST_BUY", 21)
MELON2_MAX = _knob("KG_MELON2_MAX", 75)
MELON2_DAY = _knob("KG_MELON2_DAY", 11)
MELON2_NEAR = _knob("KG_MELON2_NEAR", 0)
MELON_PLANT_PRIO = _knob("KG_MELON_PLANT_PRIO", 90)
MELON2_RESERVE_DAY = _knob("KG_MELON2_RESERVE_DAY", 8)
MELON2_FULL = _knob("KG_MELON2_FULL", 75)
STRAW_SECTOR = _knob("KG_STRAW_SECTOR", 0)
STRAW_SECTORS = _knob("KG_STRAW_SECTORS", 6)
CARROT_CAP = _knob("KG_CARROT_CAP", 12)
CARROT_MIN_CAFES = _knob("KG_CARROT_MIN_CAFES", 2)
CARROT_LAST_PLANT = _knob("KG_CARROT_LAST", 25)
STRAW_CAP = _knob("KG_STRAW_CAP", 36)
STRAW_LAST_PLANT = _knob("KG_STRAW_LAST", 13)
STRAW_LATE_LAST = _knob("KG_STRAW_LATE_LAST", 13)
STRAW_LATE_EXTRA = _knob("KG_STRAW_LATE_EXTRA", 6)
STRAW_LATE_MIN_PRICE = _knob("KG_STRAW_LATE_MINP", 150)
OPEN_MELONS = _knob("KG_OPEN_MELONS", 11)
OPEN_SHEEP = _knob("KG_OPEN_SHEEP", 3)
SHEEP_BASE = _knob("KG_SHEEP_BASE", 3)          # minimum herd from SHEEP_BASE_DAY on (fertilizer + wool)
SHEEP_BASE_DAY = _knob("KG_SHEEP_BASE_DAY", 3)
OPEN_COWS = _knob("KG_OPEN_COWS", 1)
# v15 opening (2 cows + 2 sheep, no forced third sheep): faithful rematches live 169/172,
# top corpus 209/222 vs 166/203 with the v12 opening (3 sheep + 1 cow).  Applied when
# KG_ADAPT_OPENING=1 (default follows KG_ADAPT so main.py stays v12 by default).
ADAPT_OPENING = _knob("KG_ADAPT_OPENING", -1)
if ADAPT_OPENING == -1:
    ADAPT_OPENING = ADAPT
SHEEP_YARN_BASE = OPEN_SHEEP        # base of the "3 + 4 per yarn store" herd formula (v12: the 3 opening sheep)
if ADAPT_OPENING:
    if "KG_OPEN_COWS" not in os.environ:
        OPEN_COWS = 2
    if "KG_OPEN_SHEEP" not in os.environ:
        OPEN_SHEEP = 2
    if "KG_SHEEP_BASE" not in os.environ:
        SHEEP_BASE = 2
    SHEEP_YARN_BASE = _knob("KG_SHEEP_YARN_BASE", 2)
COW_RAMP = [int(x) for x in str(_knob("KG_COW_RAMP", "1,1,1,1,2,3,5,7")).replace(".", ",").split(",") if x]
OPEN_WHEAT_SEEDS = _knob("KG_OPEN_WSEEDS", 6)
OPEN_WHEAT_FEED = _knob("KG_OPEN_WFEED", 4)
OPEN_HIRES = _knob("KG_OPEN_HIRES", 5)


class State:
    def __init__(self):
        self.last_step = -1
        self.roles = {}            # (x,y) -> "ANIMAL" | "MELON" | "STRAW" | "WHEAT"
        self.targets = {}          # unit idx -> (x,y) target last turn
        self.anchors = {}          # unit idx -> (x,y) territory anchor for today
        self.tile_sec = {}         # v13 sectors: tile -> sector id
        self.unit_sec = {}         # v13 sectors: unit idx -> sector id
        self.sec_day = -1
        self.duty = {}             # unit idx -> set of far must-do tiles assigned this morning
        self.duty_day = -1
        self.territory = {}        # (x,y) -> unit idx that owns the tile today
        self.terr_hour = -1
        self.terr_hands = -1
        self.routes = {}           # v13: unit idx -> [Stop] for today
        self.vroutes = []          # v13: routes waiting for hands still to be hired
        self.route_day = -1
        self.route_skip = {}       # v13: unit idx -> job keys skipped today (missing item)
        self.route_hands = -1      # v13: hands seen last hour (virtual routes dissolve when hiring stops)
        self.first_yarn_day = None # v14: day the first yarn store was seen
        self.tomato_target = 0     # v15: tomato plants wanted (pizza / farmers-market draws)
        self.prev_inv = None       # v16 market-aware: market inventory seen last step
        self.prev_step = -1
        self.last_orders = []      # my market orders of the previous step
        self.opp_daily = {}        # item -> {day: units the opponent sold} (inferred)
        self.milk_rich = False     # v14 draw-adaptive flags
        self.straw_rich = False
        self.melon_tiles = []
        self.melon_plant_day = 0   # day on which melon tiles may be planted
        self.melon_wave2 = False
        self.melon_wave2_n = 12
        self.plan_day = -1
        self.hires_wanted = 0
        self.straw_target = 0
        self.carrot_target = 0
        self.cow_target = OPEN_COWS
        self.sheep_target = OPEN_SHEEP
        self.bought_today = {}
        self.pending_animals = {}  # animal -> count bought but not yet placed (approx)
        self.log = []


_STATES = {}


def get_state(player, step):
    st = _STATES.get(player)
    if st is None or step <= 0 or step < st.last_step:
        st = State()
        _STATES[player] = st
    st.last_step = step
    return st


# ----------------------------------------------------------------------------
# observation helpers
# ----------------------------------------------------------------------------
def G(d, k, default=None):
    if d is None:
        return default
    try:
        v = d.get(k, default) if hasattr(d, "get") else getattr(d, k, default)
    except Exception:
        v = default
    return default if v is None else v


def is_plant(t):
    return isinstance(t, dict) and t.get("kind") == "PLANT"


def is_weed(t):
    return isinstance(t, dict) and t.get("kind") == "WEED"


def is_struct(t):
    return isinstance(t, dict) and t.get("kind") in ("COOP", "PASTURE")


def has_animal(t):
    return isinstance(t, dict) and "animal" in t and t.get("animal") is not None


def is_empty_struct(t):
    return is_struct(t) and not has_animal(t)


class World:
    pass


def parse(obs):
    W = World()
    W.player = int(G(obs, "player", 0))
    W.step = int(G(obs, "step", 0))
    W.day = int(G(obs, "day", W.step // TPD))
    W.hour = int(G(obs, "hour", W.step % TPD))
    farms = G(obs, "farms", [])
    W.farm = farms[W.player]
    W.opp = farms[1 - W.player] if len(farms) > 1 else None
    W.money = float(G(W.farm, "money", 0))
    W.tiles = G(W.farm, "tiles", [])
    W.farmer = tuple(G(W.farm, "farmer", (4, 4)))
    W.hands = [tuple(h) for h in G(W.farm, "hands", [])]
    W.units = [W.farmer] + W.hands
    W.unlocked = list(G(W.farm, "unlocked_quadrants", ["NW"]))
    W.hires_today = int(G(W.farm, "hires_today", 0))
    priv = G(obs, "private", {})
    W.shed = dict(G(priv, "shed", {}))
    W.seeds = dict(G(priv, "seeds", {}))
    invs = G(priv, "inventories", [{}])
    W.invs = [dict(iv) for iv in invs]
    while len(W.invs) < len(W.units):
        W.invs.append({})
    market = G(obs, "market", {})
    W.inv = dict(G(market, "inventory", {}))
    W.prices = dict(G(market, "prices", {}))
    town = G(obs, "town", {})
    W.shops = list(G(town, "unlocked_shops", []))
    W.steps_left = LAST_STEP - W.step          # actions after this one that still execute
    return W


def tile_at(W, p):
    x, y = p
    if 0 <= x < BOARD and 0 <= y < BOARD:
        return W.tiles[y][x]
    return "LOCKED"


def _shop_frac(product):
    """Share of shop types that buy `product` (draws are with replacement): 3/8 milk, 4/8 strawberry."""
    return sum(1 for prods in SHOPS.values() if product in prods) / float(max(1, len(SHOPS)))


def shop_count(W, product):
    n = 0
    for s in W.shops:
        prods = SHOPS.get(s, [])
        if product in prods:
            n += 2 if len(prods) == 1 else 1
    return n


def shed_total(W):
    return sum(v for v in W.shed.values() if v)


# ----------------------------------------------------------------------------
# tile roles / layout
# ----------------------------------------------------------------------------
def unlocked_tiles(W):
    out = []
    for y in range(BOARD):
        for x in range(BOARD):
            if W.tiles[y][x] != "LOCKED":
                out.append((x, y))
    return out


def assign_roles(st, W):
    """(Re)compute roles for all unlocked tiles. Animal zone = nearest tiles to
    center, then strawberry ring, then wheat. Melon tiles are fixed on day 0."""
    tiles = unlocked_tiles(W)
    n_animal = max(st.cow_target + st.sheep_target + getattr(st, "goose_target", 0), 4)
    if W.day >= 6:
        n_animal = max(n_animal, ANIMAL_ZONE)
    elif W.day >= 2:
        n_animal = max(n_animal, 8)
    # existing animal structures always keep the ANIMAL role
    ordered = sorted(tiles, key=lambda t: (center_dist(t), euclid2(t), t[1], t[0]))
    roles = {}
    if W.day == 0 and not st.melon_tiles:
        # melons on the farthest NW tiles (rows 0-2 mostly)
        nw = [t for t in ordered if quadrant(*t) == "NW"]
        far = sorted(nw, key=lambda t: (-center_dist(t), t[1], t[0]))
        if MELON_NEAR_MAX > 0:
            # the day-10 melon dump is a first-seller race (sq glut, no drain): the
            # top agents keep their melons within ~5 tiles of the shed and sell from
            # h9; the far corners cost ~3 hours and ~$40/unit in every game
            near = [t for t in nw if MELON_NEAR_MIN <= center_dist(t) <= MELON_NEAR_MAX]
            near.sort(key=lambda t: (center_dist(t), t[1], t[0]))
            if len(near) >= OPEN_MELONS:
                far = near
        st.melon_tiles = far[:OPEN_MELONS]
    if st.melon_wave2 and (not st.melon_tiles or (W.day == st.melon_plant_day and MELON2_NEAR == 1)):
        def free_today(t):
            tl = tile_at(W, t)
            if tl is None or is_weed(tl):
                return True
            if is_plant(tl):
                age = W.day - int(tl.get("planted_day", W.day))
                if tl.get("crop") == "MELON" and age >= 10:
                    return True            # first-wave tile, harvested today
                if tl.get("crop") == "WHEAT" and age >= CROPS["WHEAT"]["maxday"]:
                    return True            # harvested today
            return False
        planted_now = [t for t in tiles if is_plant(tile_at(W, t)) and tile_at(W, t).get("crop") == "MELON"
                       and int(tile_at(W, t).get("planted_day", -1)) == st.melon_plant_day]
        cand = [t for t in tiles if free_today(t) and center_dist(t) > 2.5 and t not in planted_now]
        if MELON2_NEAR:
            # nearest tiles: the day-20 harvest reaches the shed (and the market)
            # hours before the opponents' dump
            cand.sort(key=lambda t: (center_dist(t), euclid2(t), t[1], t[0]))
        else:
            cand.sort(key=lambda t: (-center_dist(t), t[1], t[0]))
        st.melon_tiles = planted_now + cand[:max(0, st.melon_wave2_n - len(planted_now))]
    melon_set = set(st.melon_tiles)
    animal_left = n_animal
    for t in ordered:
        tl = tile_at(W, t)
        if is_struct(tl):
            roles[t] = "ANIMAL"
            animal_left -= 1
    for t in ordered:
        if t in roles:
            continue
        if t in melon_set:
            tl = tile_at(W, t)
            if (st.melon_plant_day == W.day or (is_plant(tl) and tl.get("crop") == "MELON")
                    or (st.melon_wave2 and W.day < st.melon_plant_day)):
                roles[t] = "MELON"
                continue
    # animal zone: strictly the nearest tiles to the shed (regardless of what
    # currently grows there -- crops there are not replanted and the tile is
    # converted to a pasture once free).  Keeps every daily animal visit short.
    def free(t):
        tl = tile_at(W, t)
        return tl is None or is_weed(tl)
    cand = [t for t in ordered if t not in roles]
    n_zone = sum(1 for r in roles.values() if r == "ANIMAL")
    for t in cand:
        if animal_left <= 0:
            break
        tl = tile_at(W, t)
        # never claim a growing strawberry (17-day crop) for the zone (nor a tomato)
        if is_plant(tl) and (tl.get("crop") == "STRAWBERRY" or (ZONE_SKIP_TOMATO and tl.get("crop") == "TOMATO")):
            continue
        # beyond the core zone only accept tiles that are still close to the
        # shed -- a far pasture means a long daily feed trip and missed feeds
        far_lim = ANIMAL_FAR_LIMIT if not getattr(st, "yarn_shift", False) else max(ANIMAL_FAR_LIMIT, ANIMAL_FAR_YARN)
        zone_more = getattr(st, "milk_rich", False) or ADAPT_ZONE_ANY == 1 or (
            ADAPT_ZONE_ANY == 2 and shop_count(W, "WOOL") // 2 >= 2)
        if ADAPT and zone_more and st.cow_target + st.sheep_target > ANIMAL_ZONE:
            far_lim = max(far_lim, ADAPT_FAR)
        if n_zone >= ANIMAL_ZONE and center_dist(t) > far_lim:
            continue
        roles[t] = "ANIMAL"
        animal_left -= 1
        n_zone += 1
    # strawberries: existing plants first, then free tiles nearest the centre
    straw_left = st.straw_target
    for t in ordered:
        if t not in roles:
            tl = tile_at(W, t)
            if is_plant(tl) and tl.get("crop") == "STRAWBERRY":
                roles[t] = "STRAW"
                straw_left -= 1
    rest = [t for t in ordered if t not in roles]
    if STRAW_SECTOR:
        # fill strawberries sector by sector (angular wedges around the shed) so
        # that plants of the same age -- which need water on the same days --
        # sit next to each other and one hand can sweep them.
        def skey(t):
            ang = math.atan2(t[1] - CENTER[1], t[0] - CENTER[0])
            sec = int((ang + math.pi) / (2 * math.pi) * STRAW_SECTORS) % STRAW_SECTORS
            return (0 if free(t) else 1, sec, center_dist(t))
    else:
        def skey(t):
            return (0 if free(t) else 1, center_dist(t))
    for t in sorted(rest, key=skey):
        if straw_left <= 0:
            break
        roles[t] = "STRAW"
        straw_left -= 1
    # carrots (only when pet cafes create demand): existing carrot plants keep
    # the role, then free tiles nearest the centre
    carrot_left = st.carrot_target
    for t in ordered:
        if t not in roles:
            tl = tile_at(W, t)
            if is_plant(tl) and tl.get("crop") == "CARROT":
                roles[t] = "CARROT"
                carrot_left -= 1
    rest = [t for t in ordered if t not in roles]
    for t in sorted(rest, key=lambda t: (0 if free(t) else 1, center_dist(t))):
        if carrot_left <= 0:
            break
        roles[t] = "CARROT"
        carrot_left -= 1
    # tomatoes (pizza shops / farmers markets drain 6/day each and nobody supplies
    # them: the hinge curve pays $150-500 once the town is 300+ short)
    tomato_left = getattr(st, "tomato_target", 0)
    for t in ordered:
        if t not in roles:
            tl = tile_at(W, t)
            if is_plant(tl) and tl.get("crop") == "TOMATO":
                roles[t] = "TOMATO"
                tomato_left -= 1
    rest = [t for t in ordered if t not in roles]
    for t in sorted(rest, key=lambda t: (0 if free(t) else 1, center_dist(t))):
        if tomato_left <= 0:
            break
        roles[t] = "TOMATO"
        tomato_left -= 1
    for t in ordered:
        if t not in roles:
            roles[t] = "WHEAT"
    st.roles = roles


# ----------------------------------------------------------------------------
# daily macro plan
# ----------------------------------------------------------------------------
def count_animals(W, kind=None, farm=None):
    farm = farm if farm is not None else W.farm
    tiles = G(farm, "tiles", [])
    n = 0
    for row in tiles:
        for t in row:
            if has_animal(t) and (kind is None or t.get("animal") == kind):
                n += 1
    return n


def count_crop(W, crop, farm=None):
    farm = farm if farm is not None else W.farm
    n = 0
    for row in G(farm, "tiles", []):
        for t in row:
            if is_plant(t) and t.get("crop") == crop:
                n += 1
    return n


def update_targets(st, W):
    day = W.day
    yarn = shop_count(W, "WOOL") // 2          # yarn store counts double
    milk_shops = shop_count(W, "MILK")
    straw_shops = shop_count(W, "STRAWBERRY")
    # cows
    if day < 8:
        ramp = COW_RAMP
        if ADAPT and ADAPT_COW_RAMP_MILK and milk_shops >= ADAPT_COW_RAMP_MILK_MIN:
            # milk draw seen early: the 09-07 leaders hold 6 cows by day 6 and 11 by
            # day 8 (milk 414u vs my 288u in a 5-shop draw); the default ramp is 2/5/7
            ramp = ADAPT_COW_RAMP_MILK
        st.cow_target = max(OPEN_COWS, ramp[min(day, len(ramp) - 1)])
    else:
        st.cow_target = COW_BASE if milk_shops >= 1 else 6
        if ADAPT and milk_shops >= 2:
            # two milk shops by day 8: the expected final count is ~4 (P(>=4) ~ 73%)
            # and the milk market stays under-supplied -> the tetsuya-style 10-cow default
            st.cow_target = max(st.cow_target, ADAPT_COW_M2)
        if day >= 12 and milk_shops >= 4:
            st.cow_target = max(st.cow_target, 9)
        # strong milk demand (many milk shops, high price): expand the herd
        extra = COW_PER_MILK_SHOP * max(0, milk_shops - 2)
        if extra > 0 and W.prices.get("MILK", 160) >= COW_MIN_PRICE:
            st.cow_target = min(COW_CAP, st.cow_target + extra)
    # yarn-heavy draw seen early (a yarn store drains 12 wool/day ~ 10 sheep;
    # a milk shop 6 milk/day ~ 5 cows): fewer cows, more sheep.  The decision
    # is taken from the first draws (day 3/6) so the pastures are filled with
    # the right animal from the start.
    if YARN_SHIFT and yarn >= YARN_SHIFT_MIN and day >= 3:
        st.yarn_shift = True
    if getattr(st, "yarn_shift", False):
        cap = YARN_COW_CAP + (YARN_COW_PER_MILK * milk_shops)
        if YARN_COW_FLOOR and milk_shops >= 1 and yarn < 2 and day >= YARN_COW_FLOOR_DAY:
            # a yarn store does not make milk worthless: keep the normal herd
            # whenever there is a milk shop (the cap only bites when there is none)
            cap = max(cap, COW_BASE)
        st.cow_target = min(st.cow_target, max(cap, owned_animals(W, "COW")))
    if day > COW_LAST_BUY:
        st.cow_target = min(st.cow_target, owned_animals(W, "COW"))
    if ADAPT and YARN_COW_CAP and yarn >= 2 and milk_shops <= YARN_COW_CAP_MILK and day >= 3:
        # yarn-heavy, milk-poor town: the leaders run 2-6 cows and 15-20 sheep; my 9-12 cows
        # sold milk at $70-100 and ate the wheat (09-07 losses: WOOL -7.5k, WHEAT -6k per loss)
        st.cow_target = min(st.cow_target, max(YARN_COW_CAP, owned_animals(W, "COW")))
    # sheep: each yarn store eats 12 wool/day (~9 sheep worth); scale with them
    per_yarn = SHEEP_PER_YARN
    if ADAPT and yarn >= 1:
        if getattr(st, "first_yarn_day", None) is None:
            st.first_yarn_day = day
        # a yarn store seen early: wool stays ~$230 for weeks (log curve below
        # I0, 13/day drain) -> a sheep bought now earns ~$200/day; late stores
        # leave too little time before the herd gluts the market
        boost_day = ADAPT_YARN_SHEEP_DAY if ADAPT_YARN_SHEEP_DAY >= 0 else ADAPT_YARN_EARLY
        if st.first_yarn_day <= boost_day:
            per_yarn = max(per_yarn, ADAPT_SHEEP_PER_YARN)
        if yarn >= 2 and getattr(st, "second_yarn_day", None) is None:
            st.second_yarn_day = day
    sheep_cap = SHEEP_CAP
    if (ADAPT and ADAPT_SHEEP_CAP_Y2 and yarn >= 2 and getattr(st, "second_yarn_day", None) is not None
            and st.second_yarn_day <= ADAPT_Y2_DAY):
        # two yarn stores drain 26 wool/day: 14 sheep (~14/day) still sell at ~$230
        sheep_cap = max(SHEEP_CAP, ADAPT_SHEEP_CAP_Y2)
    if day < 6:
        st.sheep_target = OPEN_SHEEP
    else:
        st.sheep_target = max(OPEN_SHEEP, min(sheep_cap, SHEEP_YARN_BASE + per_yarn * yarn))
    if day >= SHEEP_BASE_DAY:
        st.sheep_target = max(st.sheep_target, SHEEP_BASE)
    if ADAPT and ADAPT_SHEEP_CAP_Y3 and yarn >= 3:
        sheep_cap = max(sheep_cap, ADAPT_SHEEP_CAP_Y3)
    if getattr(st, "yarn_shift", False):
        cap = max(sheep_cap, SHEEP_CAP_YARN)
        want_s = min(cap, SHEEP_YARN_BASE + max(per_yarn, SHEEP_PER_YARN_SHIFT) * yarn)
        if day <= SHEEP_LAST_BUY:
            st.sheep_target = max(st.sheep_target, want_s)
    if day > SHEEP_LAST_BUY:
        st.sheep_target = min(st.sheep_target, owned_animals(W, "SHEEP"))
    # second melon wave.  The strong tapes replant on day 10 and dump on day 20;
    # planting a day later means selling into their dump ($115 -> $1).  So plant
    # on day 10 (tiles freed by the first harvest / near free tiles) and be first
    # to market on day 20.
    if MELON2_DAY == 10:
        if MELON2_NEAR >= 2 and day == MELON2_RESERVE_DAY and not st.melon_wave2:
            # reserve near tiles now (wheat about to be harvested / empty) so the
            # second wave sits close to the shed and reaches the market first
            st.melon_wave2 = True
            st.melon_wave2_n = 12
            st.melon_plant_day = 10
            cand = []
            for t in unlocked_tiles(W):
                if center_dist(t) <= 2.5:
                    continue
                tl = tile_at(W, t)
                ok = tl is None or is_weed(tl)
                if is_plant(tl) and tl.get("crop") == "WHEAT":
                    age = day - int(tl.get("planted_day", day))
                    ok = age >= CROPS["WHEAT"]["maxday"] - (10 - day)   # harvestable by day 10
                if ok:
                    cand.append(t)
            cand.sort(key=lambda t: (center_dist(t), euclid2(t), t[1], t[0]))
            st.melon_tiles = cand[:12]
        elif day == 10 and not st.melon_wave2 and W.money >= 1500:
            st.melon_wave2 = True
            st.melon_wave2_n = 12
            st.melon_tiles = []
            st.melon_plant_day = 10
    elif day in (10, 11) and not st.melon_wave2 and count_crop(W, "MELON") == 0:
        melon_inv = W.inv.get("MELON", MARKET_I0)
        # draw-adaptive: when the draw calls for a bigger herd / field the tiles
        # and labour of a second melon wave are worth more as cows / strawberries
        busy = ADAPT and ADAPT_MELON2_SKIP and (getattr(st, "milk_rich", False) or getattr(st, "straw_rich", False)
                                                 or shop_count(W, "MILK") >= ADAPT_MELON2_SKIP_M
                                                 or (getattr(st, "first_yarn_day", None) is not None
                                                     and st.first_yarn_day <= ADAPT_YARN_EARLY))
        if melon_inv <= MARKET_I0 + MELON2_MAX and W.money >= 2000 and not busy:
            st.melon_wave2 = True
            st.melon_wave2_n = 12 if melon_inv <= MARKET_I0 + MELON2_FULL else 6
            st.melon_tiles = []
            st.melon_plant_day = day
    # strawberries: plant early (they sell at scarcity prices from day ~13 on)
    # and grow the field as land / cash arrive.  Shops modulate the final size.
    if day < 3:
        tgt = 0
    elif day <= 5:
        tgt = 6
    elif day <= 7:
        tgt = 18
    elif day <= 9:
        tgt = 30
    else:
        tgt = 36
    if day >= 9:
        tgt += 3 * straw_shops - (4 if straw_shops == 0 else 0)
        tgt = max(24, min(STRAW_CAP, tgt))
    n_now = count_crop(W, "STRAWBERRY")
    if day > STRAW_LAST_PLANT:
        tgt = n_now
        # late top-ups (2-3 productions left) only while the market is healthy
        if day <= STRAW_LATE_LAST and W.prices.get("STRAWBERRY", 120) >= STRAW_LATE_MIN_PRICE:
            tgt = min(STRAW_CAP + STRAW_LATE_EXTRA, n_now + STRAW_LATE_EXTRA)
    if ADAPT and day >= 3:
        # expected final shop counts: draws so far + prior for the remaining
        # (strawberry in 4/8 shop types, milk in 3/8); the decision is sticky
        k = min(MAX_SHOPS, day // SHOP_UNLOCK_INT)
        m_exp = milk_shops + _shop_frac("MILK") * (MAX_SHOPS - k)
        s_exp = straw_shops + _shop_frac("STRAWBERRY") * (MAX_SHOPS - k)
        if m_exp >= ADAPT_MILK_EXP:
            st.milk_rich = True
        if s_exp >= ADAPT_STRAW_EXP:
            st.straw_rich = True
        if getattr(st, "milk_rich", False) and (ADAPT_MILK_OVER_YARN or not getattr(st, "yarn_shift", False)):
            want = 12 + ADAPT_COW_PER_MILK * max(0, milk_shops - 3)
            want = min(ADAPT_COW_CAP, want)
            if MARKET_AWARE and day >= 10:
                # milk hole per day = drain - rival's supply; my cows give ~0.9/day each
                drain_m = daily_drain(W, "MILK")
                room = drain_m - opp_rate(st, "MILK", day)
                cap_cows = int(max(owned_animals(W, "COW"), room / 0.9))
                want = min(want, max(cap_cows, 8))
            if day <= ADAPT_COW_LAST and day >= 8:
                st.cow_target = max(st.cow_target, want)
            elif day > ADAPT_COW_LAST:
                st.cow_target = min(st.cow_target, owned_animals(W, "COW"))
        if OPP_AWARE and W.opp is not None and OPP_COW_DAY <= day <= COW_LAST_BUY:
            # milk share game against the observed rival herd: the town drains
            # 1 + 6/shop per day; each cow gives ~1.2/day once producing.  Room
            # for my herd = drain(expected shops) / 1.2 - rival cows.
            opp_cows = count_animals(W, "COW", farm=W.opp)
            drain_exp = 1.0 + 6.0 * m_exp
            room = int(drain_exp / OPP_MILK_PER_COW) - opp_cows
            if OPP_COW_UP and opp_cows <= OPP_COW_RIVAL and milk_shops >= 1:
                st.cow_target = max(st.cow_target, min(OPP_COW_UP, room))
            if OPP_COW_DOWN:
                st.cow_target = min(st.cow_target, max(OPP_COW_FLOOR, room, owned_animals(W, "COW")))
        if day <= STRAW_LAST_PLANT:
            if getattr(st, "milk_rich", False) and not getattr(st, "straw_rich", False):
                tgt = min(tgt, ADAPT_STRAW_MILK)
            elif getattr(st, "straw_rich", False):
                cap = ADAPT_STRAW_RICH2 if s_exp >= ADAPT_STRAW_EXP + 0.5 else ADAPT_STRAW_RICH
                tgt = max(tgt, min(cap, 36 + 3 * straw_shops))
            # strawberry-poor draw: no strawberry shop among the first draws ->
            # 36 plants glut a 7/day market against a 24-plant opponent (tetsuya
            # games -8k); the freed tiles grow wheat instead
            if ADAPT_STRAW_YARN and getattr(st, "first_yarn_day", None) is not None and st.first_yarn_day <= ADAPT_YARN_EARLY:
                # early yarn store -> 10 sheep: labour and near tiles go to the herd
                # (tetsuya plants 24-30 strawberries in these draws)
                tgt = min(tgt, ADAPT_STRAW_YARN)
            if ADAPT_STRAW_POOR and straw_shops == 0 and day >= 9:
                tgt = min(tgt, ADAPT_STRAW_POOR)
            elif ADAPT_STRAW_POOR and straw_shops == 0 and day >= 6:
                tgt = min(tgt, ADAPT_STRAW_POOR6)
            elif ADAPT_STRAW_POOR1 and straw_shops == 1 and day >= 9:
                tgt = min(tgt, ADAPT_STRAW_POOR1)
            if OPP_AWARE and OPP_STRAW_MATCH and W.opp is not None and day >= OPP_STRAW_DAY:
                opp_st = count_crop(W, "STRAWBERRY", farm=W.opp)
                if opp_st >= tgt + OPP_STRAW_MARGIN:
                    tgt = max(tgt, min(OPP_STRAW_CAP, opp_st))
    st.straw_target = max(tgt, n_now)
    # carrots: pet cafes eat 12/day each (hinge scarcity curve -> price climbs
    # steeply once the town has drained more than 450); farmers markets 6/day.
    pet_cafes = sum(1 for s in W.shops if s == "PET_CAFE")
    if day <= CARROT_LAST_PLANT and pet_cafes >= CARROT_MIN_CAFES:
        st.carrot_target = min(CARROT_CAP, 4 * pet_cafes)
    else:
        st.carrot_target = 0
    # geese: eggs are ~$50 whatever happens (log glut 0.2) and $56-80 with egg outlets; the 09-07
    # leaders run 2 geese with one egg outlet and 3 with two or more (mandgeee 79 eggs)
    st.goose_target = 0
    if ADAPT and ADAPT_GEESE and day <= GEESE_LAST:
        egg_sh = shop_count(W, "EGG")
        if egg_sh >= 2:
            st.goose_target = GEESE_EGG2
        elif egg_sh >= 1:
            st.goose_target = GEESE_EGG1
    st.goose_target = max(st.goose_target, owned_animals(W, "GOOSE")) if day <= GEESE_LAST else owned_animals(W, "GOOSE")
    st.tomato_target = 0
    tomato_shops = shop_count(W, "TOMATO")
    if ADAPT and tomato_shops >= ADAPT_TOMATO_MIN and day <= ADAPT_TOMATO_DECIDE:
        # a tomato plant gives 4 daily productions from age 8 (+fertilizer x2) and
        # nobody supplies tomatoes: the hinge curve pays $150-500 once the town is
        # 300+ short.  Size the field by the deficit the known shops will have
        # built by day 24 (6/day each from their unlock day, +1/day base).
        est = 24
        for i, sh in enumerate(W.shops):
            if "TOMATO" in SHOPS.get(sh, []):
                est += 6 * max(0, 24 - SHOP_UNLOCK_INT * (i + 1))
        if MARKET_AWARE and day >= 6:
            # what the town will still be short of by day 24: the hole already dug
            # (I0 - inventory) plus the remaining drain, minus what the opponent is
            # already supplying per day (inferred); the formula above assumes an
            # untouched market and no rival supply
            hole = MARKET_I0 - int(W.inv.get("TOMATO", MARKET_I0))
            fut = 0
            for i, sh in enumerate(W.shops):
                if "TOMATO" in SHOPS.get(sh, []):
                    fut += 6 * max(0, 24 - max(day, SHOP_UNLOCK_INT * (i + 1)))
            fut += max(0, 24 - day)
            est = hole + fut - opp_rate(st, "TOMATO", day) * max(0, 24 - day)
        n = 0
        if est >= 400:
            n = 32
        elif est >= 320:
            n = 24
        elif est >= 240:
            n = 16
        elif est >= ADAPT_TOMATO_MIN_DEF:
            n = 8
        st.tomato_plan = max(getattr(st, "tomato_plan", 0), min(ADAPT_TOMATO_CAP, n))
    if ADAPT and day <= ADAPT_TOMATO_LAST:
        st.tomato_target = getattr(st, "tomato_plan", 0)
    n_tom = count_crop(W, "TOMATO")
    st.tomato_target = max(st.tomato_target, n_tom)
    carrot_drain = 13 * pet_cafes + (6 * sum(1 for s in W.shops if s == "FARMERS_MARKET") if ADAPT_CARROT_FARM else 0)
    if ADAPT and (pet_cafes >= 2 or (ADAPT_CARROT_FARM and carrot_drain >= 19)) and ADAPT_CARROT_DAY <= day <= CARROT_LAST_PLANT:
        # pet cafes drain 13 carrots/day each and the hinge curve pays $100-250
        # once the town is 500+ short: a late carrot wave (3-day crop) is worth
        # far more than wheat on the same tiles
        want_c = min(ADAPT_CARROT_CAP, ADAPT_CARROT_PER_CAFE * (pet_cafes - 1))
        if ADAPT_CARROT_FARM:
            # farmers markets drain carrots too (6/day each vs 13 for a pet cafe)
            want_c = max(want_c, min(ADAPT_CARROT_CAP, int(ADAPT_CARROT_PER_CAFE * (carrot_drain / 13.0 - 1))))
        if MARKET_AWARE:
            # the rival's carrot supply fills the same hole: each 4 units/day they
            # sell is one plant's worth of the wave
            want_c = max(0, want_c - int(opp_rate(st, "CARROT", day) / 4.0))
        st.carrot_target = max(st.carrot_target, want_c)


def desired_hands(st, W):
    """Estimate today's workload and convert to hires."""
    day = W.day
    if day == 0:
        return 5
    if day == 1:
        return HANDS_D1
    if day >= LAST_DAY:
        return 8
    work = 0.0
    n_animals = count_animals(W)
    work += n_animals * 4.0
    plants = 0
    for row in W.tiles:
        for t in row:
            if is_plant(t):
                plants += 1
    work += plants * 1.25
    # planting / building work
    empties = sum(1 for t in unlocked_tiles(W) if tile_at(W, t) is None)
    work += min(empties, 20) * 1.0
    if day == 8:
        work += 40
    walking = HIRE_WALK
    n = int(math.ceil(work * walking / 22.0)) - 1
    if day <= 5:
        lo, hi = HANDS_LO_EARLY, max(5, HANDS_LO_EARLY)
    elif day <= 7:
        lo, hi = EARLY_HANDS_LO, 9
    elif day <= 9:
        lo, hi = 10, 12
    elif day <= 27:
        lo, hi = MIN_HANDS, MAX_HANDS
        if 10 <= day <= 25 and W.money >= 6000:
            hi = MAX_HANDS_RICH
        lo = min(lo, hi)
    else:
        lo, hi = 9, 11
    n = max(lo, min(hi, n))
    return n


def plan_day(st, W):
    """Called at hour 0 (or first turn we see of a day)."""
    st.plan_day = W.day
    update_targets(st, W)
    if MARKET_TAPE and (not TAPE_OPENING or W.day >= TAPE_OPENING_DAYS or getattr(st, "tape_off", False)):
        _mtape_pick(st, W)
        _mtape_targets(st, W)
    assign_roles(st, W)
    st.hires_wanted = desired_hands(st, W)
    if HIRE_MODE == "measured" and 2 <= W.day < LAST_DAY:
        # count today's actual work from the job list (bundles count all actions)
        try:
            jobs0 = build_jobs(st, W)
            n_act = sum(len(j.actions) for j in jobs0)
            # harvest/plant/collect that appear later in the day
            n_act += count_animals(W) * 1.2 + count_crop(W, "STRAWBERRY") * 0.6 + 6
            need = int(math.ceil(n_act * HIRE_MEAS_WALK / 22.0)) - 1
            lo, hi = (3, 5) if W.day <= 5 else ((7, 9) if W.day <= 7 else (HIRE_MEAS_MIN, MAX_HANDS))
            if W.day == 8:
                lo = max(lo, 10)
            if 10 <= W.day <= 25 and W.money >= 6000:
                hi = MAX_HANDS_RICH
            st.hires_wanted = max(lo, min(hi, need))
        except Exception:
            pass
    st.anchors = compute_anchors(W, st.hires_wanted)
    if SECTORS:
        st.tile_sec, st.unit_sec = compute_sectors(st, W, st.hires_wanted)
        st.sec_day = W.day
    st.territory = {}
    st.terr_hour = -1
    st.bought_today = {}


# ----------------------------------------------------------------------------
# job model
# ----------------------------------------------------------------------------
class Job:
    __slots__ = ("tile", "actions", "prio", "need", "need_n", "key", "taken", "kind")

    def __init__(self, tile, actions, prio, need=None, need_n=1, kind=""):
        self.tile = tile
        self.actions = actions
        self.prio = prio
        self.need = need
        self.need_n = need_n
        self.key = (tile, kind)
        self.taken = False
        self.kind = kind


def water_wanted(crop, age, tl, day):
    """Should this plant be watered today?"""
    if age == 0:
        return True
    cu = int(tl.get("consecutive_unwatered", 0))
    if cu >= 1:
        return True                       # would die tonight otherwise
    if crop == "WHEAT":
        return age in (2, 3, 4)
    if crop == "CARROT":
        return age in (2, 3)
    if crop == "MELON":
        if age >= 6:
            return True                   # bonus window 6..10 (+1 per watered day)
        return age % 2 == 0 if not IDLE_WATER else False
    if crop == "STRAWBERRY":
        if age >= 9:
            return age % 2 == 1 or age <= 9
        return age % 2 == 0 if not IDLE_WATER else False
    if crop == "TOMATO":
        return True
    return age % 2 == 0


STRAW_MIN_YIELD = _knob("KG_STRAW_MIN_YIELD", 1)
PREM_DROP_SCALE = _knob("KG_PREM_DROP_SCALE", 2.0)


def strawberry_harvest_wanted(age, yield_units, day):
    # strawberry price collapses linearly with supply: sell as early as we can,
    # so harvest whatever is on the plant whenever it is ready.
    if yield_units <= 0 or age < 10:
        return False
    if age >= 16 or day >= LAST_DAY or yield_units >= STRAW_MIN_YIELD:
        return True
    # a production tonight would exceed the held cap of 4
    return age in (11, 13, 15) and yield_units >= 3


def next_production_day(tl, kind):
    a = ANIMALS[kind]
    placed = int(tl.get("placed_day", 0))
    return placed, a


def build_jobs(st, W):
    jobs = []
    day, hour = W.day, W.hour
    endgame = day >= LAST_DAY
    shed_wheat = int(W.shed.get("WHEAT", 0))
    shed_fert = int(W.shed.get("FERTILIZER", 0))
    roles = st.roles
    melon_day = day >= 8
    build_left = structures_needed(st, W)
    coop_left = 0
    if ADAPT and ADAPT_GEESE:
        empty_coops = sum(1 for row in W.tiles for t in row if is_empty_struct(t) and t.get("kind") == "COOP")
        coop_left = max(0, getattr(st, "goose_target", 0) - owned_animals(W, "GOOSE")) - empty_coops
        coop_left = max(0, coop_left)
    plant_wheat_prio = 66 if day >= PLANT_T2_DAY else 58

    # count seeds available for planting
    seeds = dict(W.seeds)
    animals_in_shed = {a: int(W.shed.get(a, 0)) for a in ANIMALS}
    # animals carried by units count too
    # strawberry tiles held empty today: bounded by seeds on hand + what today's cash buys
    hold_left = 0
    if STRAW_TILE_HOLD:
        hold_left = int(W.seeds.get("STRAWBERRY", 0)) + int(max(0, W.money - 200) // CROPS["STRAWBERRY"]["seed"])
        hold_left = min(hold_left, max(0, st.straw_target - count_crop(W, "STRAWBERRY")))
    wheat_available = shed_wheat
    fert_available = shed_fert
    for iv in W.invs:
        for a in ANIMALS:
            animals_in_shed[a] += int(iv.get(a, 0))
        wheat_available += int(iv.get("WHEAT", 0))
        fert_available += int(iv.get("FERTILIZER", 0))

    for (x, y), role in roles.items():
        tl = W.tiles[y][x]
        t = (x, y)
        if tl == "LOCKED":
            continue
        # ---------------- plants ----------------
        if is_plant(tl):
            crop = tl.get("crop")
            age = day - int(tl.get("planted_day", day))
            yu = int(tl.get("yield_units", 0))
            watered = bool(tl.get("watered_today", False))
            fert_until = int(tl.get("fertilized_until_day", -1))
            acts = []
            prio = 0
            if (ADAPT and ADAPT_DIG and role == "ANIMAL" and crop == "WHEAT" and build_left > 0
                    and getattr(st, "milk_rich", False) and age <= 2 and not endgame and day <= SHEEP_LAST_BUY):
                # milk-rich draw: a pasture slot is worth far more than this
                # young wheat -> clear the tile now instead of waiting for harvest
                build_left -= 1
                jobs.append(Job(t, ["DIG"], 66, kind="dig"))
                continue
            if crop == "MELON":
                # engine: HARVEST is a no-op before first_yield_day (age 10)
                if age >= 10:
                    acts2 = []
                    if not watered and yu < 6 and age <= 12 and not endgame:
                        acts2.append("WATER")
                    acts2.append("HARVEST")
                    jobs.append(Job(t, acts2, 100, kind="harvest"))
                    continue
                if not watered and water_wanted(crop, age, tl, day):
                    acts.append("WATER")
                    prio = max(prio, 76 if age < 6 else 79)
                    if int(tl.get("consecutive_unwatered", 0)) >= 1:
                        prio = max(prio, 82 if hour < DYING_HOUR else 91)
                    if hour >= URGENT_HOUR:
                        prio = max(prio, 92)
                if acts:
                    jobs.append(Job(t, acts, prio, kind="tend"))
                continue
            if crop == "WHEAT" or crop == "CARROT":
                mx = CROPS[crop]["maxday"]
                if age >= mx or (endgame and yu > 0 and age >= CROPS[crop]["first"]):
                    acts2 = []
                    win_start = (mx + 1) // 2
                    if (not watered and win_start <= age <= mx and yu < CROPS[crop]["max_yield"]
                            and not (endgame and hour >= 21)):
                        acts2.append("WATER")      # +1 unit before harvesting
                    acts2.append("HARVEST")
                    pr = 84 if age >= mx else 70
                    if age > mx:
                        pr = 96
                    jobs.append(Job(t, acts2, pr, kind="tend"))
                    continue
                if not watered and water_wanted(crop, age, tl, day):
                    pr = 74
                    if WHEAT_WIN_PRIO and crop == "WHEAT" and (mx + 1) // 2 <= age <= mx:
                        pr = WHEAT_WIN_PRIO       # a missed window watering costs a unit (yield 2-3 instead of 4-6)
                    if int(tl.get("consecutive_unwatered", 0)) >= 1:
                        pr = 82 if hour < DYING_HOUR else 91
                    if hour >= URGENT_HOUR:
                        pr = max(pr, 92)
                    acts3 = ["WATER"]
                    need3 = None
                    if (WHEAT_FERT and not endgame and fert_available > 0 and age == (mx + 1) // 2
                            and int(tl.get("fertilized_until_day", -1)) < day and day <= WHEAT_FERT_LAST):
                        # a watering in the yield window adds +1 unit, +2 when fertilized
                        # (engine: bonus = 2 if fertilized_until_day >= day): one fertilizer
                        # at the window start covers the whole window -> 6 wheat instead of
                        # 4 per tile (the 09-07 leaders buy $2k of feed, I bought $12k)
                        acts3 = ["FERTILIZE", "WATER"]
                        need3 = "FERTILIZER"
                        fert_available -= 1
                    jobs.append(Job(t, acts3, pr, need=need3, kind="tend"))
                elif (IDLE_WATER and not watered and not endgame and age < 5
                        and int(tl.get("consecutive_unwatered", 0)) == 0):
                    jobs.append(Job(t, ["WATER"], IDLE_WATER_PRIO, kind="idle"))
                continue
            if crop == "STRAWBERRY":
                acts = []
                pr = 0
                need = None
                if age >= 16:
                    # last production landed at end of age 15: harvest and clear the
                    # tile right away so wheat can follow (it would decay into a weed)
                    if yu > 0:
                        jobs.append(Job(t, ["HARVEST", "DIG"] if day <= 27 else ["HARVEST"], 88, kind="tend"))
                    elif day <= 27:
                        jobs.append(Job(t, ["DIG"], 64, kind="dig"))
                    continue
                if strawberry_harvest_wanted(age, yu, day):
                    acts.append("HARVEST")
                    pr = 76 if age < 16 else 88
                    if HARVEST_HOUR and hour < HARVEST_HOUR and age < 16 and not endgame:
                        pr = HARVEST_EARLY_PRIO       # harvest in the afternoon: carried into the night auto-drop
                    if endgame:
                        pr = 95
                # fertilizer doubles the scheduled productions (end of ages 9,11,13,15)
                # while active (3 days).  Ideal: fertilize on age 9 and 13.  Fallbacks
                # if a window was missed: age 11 (covers 11,13) / age 15 (covers 15).
                fz = False
                if not endgame and fert_until < day and fert_available > 0:
                    if age in (9, 13):
                        fz, fp = True, FERT_PRIO
                    elif age == 11 or age == 15:
                        fz, fp = True, FERT_PRIO - 6
                if fz:
                    acts.append("FERTILIZE")
                    need = "FERTILIZER"
                    fert_available -= 1
                    pr = max(pr, fp)
                if not watered and water_wanted(crop, age, tl, day) and not endgame:
                    acts.append("WATER")
                    p2 = 74
                    if age in (9, 11, 13, 15):
                        p2 = 78
                    if int(tl.get("consecutive_unwatered", 0)) >= 1:
                        p2 = 82 if hour < DYING_HOUR else 91
                    if hour >= URGENT_HOUR:
                        p2 = max(p2, 92)
                    pr = max(pr, p2)
                if acts:
                    jobs.append(Job(t, acts, pr, need=need, kind="tend"))
                if (IDLE_WATER and "WATER" not in acts and not watered and not endgame
                        and int(tl.get("consecutive_unwatered", 0)) == 0 and age <= 14
                        and (age + 1) not in (9, 11, 13, 15)):
                    jobs.append(Job(t, ["WATER"], IDLE_WATER_PRIO, kind="idle"))
                continue
            # tomato: 4 daily productions at ages 8-11 (x2 while fertilized and
            # watered); harvest whenever there is fruit, fertilize once at age 8
            if crop == "TOMATO":
                acts = []
                pr = 0
                need = None
                if yu > 0 and age >= CROPS[crop]["first"]:
                    acts.append("HARVEST")
                    pr = 76
                if not endgame and fert_until < day and fert_available > 0 and 8 <= age <= 10:
                    acts.append("FERTILIZE")
                    need = "FERTILIZER"
                    fert_available -= 1
                    pr = max(pr, FERT_PRIO)
                if not watered and not endgame and age <= 11 and (age >= 7 or int(tl.get("consecutive_unwatered", 0)) >= 1 or age % 2 == 0):
                    acts.append("WATER")
                    p2 = 78 if age >= 8 else 74
                    if int(tl.get("consecutive_unwatered", 0)) >= 1:
                        p2 = 82 if hour < DYING_HOUR else 91
                    if hour >= URGENT_HOUR:
                        p2 = max(p2, 92)
                    pr = max(pr, p2)
                if age >= 12 and yu <= 0 and day <= 27:
                    jobs.append(Job(t, ["DIG"], 64, kind="dig"))
                    continue
                if acts:
                    jobs.append(Job(t, acts, pr, need=need, kind="tend"))
                continue
            # other crops -- generic
            if yu > 0 and age >= CROPS[crop]["first"]:
                jobs.append(Job(t, ["HARVEST"], 70, kind="tend"))
            elif not watered and not endgame:
                jobs.append(Job(t, ["WATER"], 74, kind="tend"))
            continue

        # ---------------- animals ----------------
        if has_animal(tl):
            kind = tl.get("animal")
            a = ANIMALS[kind]
            placed = int(tl.get("placed_day", 0))
            yu = int(tl.get("yield_units", 0))
            fed = bool(tl.get("fed_today", False))
            cared = bool(tl.get("cared_today", False))
            pend = int(tl.get("pending_care_bonus", 0))
            fert_av = bool(tl.get("fertilizer_available", False))
            cu = int(tl.get("consecutive_unfed", 0))
            # future production days (end-of-day e) : e+1 - placed - first == k*interval, e <= 28
            prod_tonight = False
            future_prod = False
            for e in range(day, 29):
                ds = e + 1 - placed - a["first"]
                if ds >= 0 and ds % a["interval"] == 0:
                    if e == day:
                        prod_tonight = True
                    future_prod = True
                    break
            acts = []
            pr = 0
            if endgame:
                if yu > 0:
                    jobs.append(Job(t, ["HARVEST"], 95, kind="animal"))
                continue
            need_feed = (not fed) and (day < 28 or prod_tonight) and day >= 1
            if day == 28 and not fed and not prod_tonight:
                need_feed = False
            if need_feed and wheat_available <= 0:
                need_feed = False          # nothing to feed with right now
            if need_feed:
                acts.append("FEED")
                pr = 80
                if cu >= 1:
                    pr = 86
                if hour >= FEED_URGENT_HOUR:
                    pr = max(pr, 90)
                if hour >= URGENT_HOUR:
                    pr = max(pr, 93)
            if not cared and future_prod and pend < 5 and (fed or need_feed) and (day < 28 or (CARE_LAST_DAY and prod_tonight)):
                # day 28: the last production happens at the end of day 28, so a care
                # today still adds a unit for animals producing tonight (12 lost units
                # per game in the islet head-to-head)
                acts.append("CARE")
                pr = max(pr, CARE_PRIO)
            if yu > 0:
                acts.append("HARVEST")
                pr = max(pr, ANIMAL_HARVEST_PRIO if not (HARVEST_HOUR and hour < HARVEST_HOUR) else min(ANIMAL_HARVEST_PRIO, HARVEST_EARLY_PRIO))
            if fert_av and day < LAST_DAY:
                acts.append("COLLECT_FERTILIZER")
                pr = max(pr, COLLECT_PRIO)
            if acts:
                needwheat = "FEED" in acts
                jobs.append(Job(t, acts, pr, need="WHEAT" if needwheat else None, kind="animal"))
            continue

        # ---------------- empty structure ----------------
        if is_empty_struct(tl):
            kind_needed = "COW" if tl.get("kind") == "PASTURE" else "GOOSE"
            # choose which animal to place: prefer whichever we have
            for cand in (["COW", "SHEEP"] if tl.get("kind") == "PASTURE" else ["GOOSE"]):
                if animals_in_shed.get(cand, 0) > 0:
                    animals_in_shed[cand] -= 1
                    jobs.append(Job(t, ["PLACE", cand], 72, need=cand, kind="place"))
                    break
            else:
                if role != "ANIMAL" and not endgame and day < 27:
                    jobs.append(Job(t, ["DIG"], 20, kind="dig"))
            continue

        # ---------------- weeds ----------------
        if is_weed(tl):
            if not endgame and day <= 27 and role in ("WHEAT", "STRAW", "ANIMAL", "MELON", "CARROT", "TOMATO"):
                jobs.append(Job(t, ["DIG"], 60 if (role == "STRAW" and 8 <= day <= 15) else (62 if day >= 14 else 52), kind="dig"))
            continue

        # ---------------- empty tile ----------------
        if tl is None:
            if endgame:
                continue
            if role == "ANIMAL":
                # build a pasture only for animals that are on hand or about to
                # be bought (nearest free zone tiles first -- roles are ordered)
                if build_left > 0:
                    build_left -= 1
                    jobs.append(Job(t, ["BUILD_PASTURE"], 66, kind="build"))
                elif coop_left > 0:
                    coop_left -= 1
                    jobs.append(Job(t, ["BUILD_COOP"], 64, kind="build"))
                elif (day <= 1 or day >= 18) and day <= WHEAT_LAST_PLANT and seeds.get("WHEAT", 0) > 0:
                    seeds["WHEAT"] -= 1
                    jobs.append(Job(t, ["PLANT", "WHEAT"], plant_wheat_prio, kind="plant"))
                continue
            if role == "MELON":
                if day == st.melon_plant_day and seeds.get("MELON", 0) > 0:
                    seeds["MELON"] -= 1
                    # one-day window (a melon planted tomorrow is dumped into the
                    # opponents' day-20 glut) -> urgent
                    jobs.append(Job(t, ["PLANT", "MELON"], MELON_PLANT_PRIO if day > 0 else 70, kind="plant"))
                elif day > st.melon_plant_day and seeds.get("WHEAT", 0) > 0 and day <= WHEAT_LAST_PLANT:
                    seeds["WHEAT"] -= 1
                    jobs.append(Job(t, ["PLANT", "WHEAT"], plant_wheat_prio, kind="plant"))
                continue
            if role == "STRAW":
                if seeds.get("STRAWBERRY", 0) > 0 and day <= max(STRAW_LAST_PLANT, STRAW_LATE_LAST):
                    seeds["STRAWBERRY"] -= 1
                    jobs.append(Job(t, ["PLANT", "STRAWBERRY"], PLANT_STRAW_PRIO, kind="plant"))
                elif (STRAW_TILE_HOLD and day >= 3 and day <= STRAW_LAST_PLANT
                        and st.straw_target > count_crop(W, "STRAWBERRY") and hold_left > 0):
                    # leave the tile empty for the seed bought at the next market phase
                    # (a wheat replant here locked the strawberry field at 13-16 plants
                    # all game in the 09-07 head-to-heads: the seed order counts empty
                    # STRAW tiles, but the unit standing on the tile replanted wheat first)
                    hold_left -= 1
                    continue
                elif seeds.get("WHEAT", 0) > 0 and day <= WHEAT_LAST_PLANT:
                    seeds["WHEAT"] -= 1
                    jobs.append(Job(t, ["PLANT", "WHEAT"], plant_wheat_prio - 2, kind="plant"))
                continue
            if role == "TOMATO":
                if seeds.get("TOMATO", 0) > 0 and day <= ADAPT_TOMATO_LAST:
                    seeds["TOMATO"] -= 1
                    jobs.append(Job(t, ["PLANT", "TOMATO"], PLANT_STRAW_PRIO, kind="plant"))
                elif seeds.get("WHEAT", 0) > 0 and day <= WHEAT_LAST_PLANT:
                    seeds["WHEAT"] -= 1
                    jobs.append(Job(t, ["PLANT", "WHEAT"], plant_wheat_prio - 2, kind="plant"))
                continue
            if role == "CARROT":
                if seeds.get("CARROT", 0) > 0 and day <= CARROT_LAST_PLANT:
                    seeds["CARROT"] -= 1
                    jobs.append(Job(t, ["PLANT", "CARROT"], plant_wheat_prio, kind="plant"))
                elif seeds.get("WHEAT", 0) > 0 and day <= WHEAT_LAST_PLANT:
                    seeds["WHEAT"] -= 1
                    jobs.append(Job(t, ["PLANT", "WHEAT"], plant_wheat_prio - 2, kind="plant"))
                continue
            if role == "WHEAT":
                if seeds.get("WHEAT", 0) > 0 and day <= WHEAT_LAST_PLANT:
                    seeds["WHEAT"] -= 1
                    jobs.append(Job(t, ["PLANT", "WHEAT"], plant_wheat_prio, kind="plant"))
                continue
    return jobs


def owned_animals(W, kind):
    """Placed + in shed + carried."""
    n = count_animals(W, kind) + int(W.shed.get(kind, 0))
    for iv in W.invs:
        n += int(iv.get(kind, 0))
    return n


def structures_needed(st, W):
    """How many more pastures we should have ready: unplaced animals + animals
    we still intend to buy, minus empty structures already standing."""
    if W.day > SHEEP_LAST_BUY:
        return 0
    cows = owned_animals(W, "COW")
    sheep = owned_animals(W, "SHEEP")
    placed = count_animals(W)
    unplaced = (cows + sheep) - placed
    empties = 0
    for row in W.tiles:
        for t in row:
            if is_empty_struct(t):
                empties += 1
    want = max(0, st.cow_target - cows) + max(0, st.sheep_target - sheep)
    return max(0, unplaced + want - empties)


def want_more_structures(st, W):
    return structures_needed(st, W) > 0


# ----------------------------------------------------------------------------
# assignment
# ----------------------------------------------------------------------------
URGENT_HOUR = _knob("KG_URGENT_HOUR", 15)
SEED_BUFFER = _knob("KG_SEED_BUFFER", 0)
PLANT_T2_DAY = _knob("KG_PLANT_T2_DAY", 14)
PLANT_STRAW_PRIO = _knob("KG_PLANT_STRAW_PRIO", 68)
EARLY_HANDS_LO = _knob("KG_EARLY_HANDS_LO", 7)
HANDS_D1 = _knob("KG_HANDS_D1", 1)                 # hands on day 1
HANDS_LO_EARLY = _knob("KG_HANDS_LO_EARLY", 3)     # minimum hands on days 2-5
FERT_PRIO = _knob("KG_FERT_PRIO", 84)
CARE_PRIO = _knob("KG_CARE_PRIO", 70)
ANIMAL_HARVEST_PRIO = _knob("KG_AH_PRIO", 68)
COLLECT_PRIO = _knob("KG_COLLECT_PRIO", 62)
SELL_HOUR_ONLY = _knob("KG_SELL_HOUR_ONLY", 0)
SELL_HOUR_LAST = _knob("KG_SELL_HOUR_LAST", 0)
FEED_URGENT_HOUR = _knob("KG_FEED_URGENT_HOUR", 11)
MAX_HANDS = _knob("KG_MAX_HANDS", 12)
HIRE_MODE = _knob("KG_HIRE_MODE", "fixed")
HIRE_MEAS_WALK = _knob("KG_HIRE_MEAS_WALK", 2.2)
HIRE_MEAS_MIN = _knob("KG_HIRE_MEAS_MIN", 9)
HIRE_WALK = _knob("KG_HIRE_WALK", 2.1)
MIN_HANDS = _knob("KG_MIN_HANDS", 12)
MAX_HANDS_RICH = _knob("KG_MAX_HANDS_RICH", 12)
DYING_HOUR = _knob("KG_DYING_HOUR", 13)
PARTIAL_NEED = _knob("KG_PARTIAL_NEED", 1)      # non-carriers may do the item-free part of a need-job
PARTIAL_PEN = _knob("KG_PARTIAL_PEN", 60.0)
LAST_CALL = _knob("KG_LAST_CALL", 1)            # escalate important jobs that must start now to be done today
LAST_CALL_PRIO = _knob("KG_LAST_CALL_PRIO", 78)
LAST_CALL_SLACK = _knob("KG_LAST_CALL_SLACK", 0)   # hours of slack before the true last moment
LAST_CALL_EXTRA = _knob("KG_LAST_CALL_EXTRA", 0.0) # extra score beyond tier 3 (beat on-tile chains: > ON_TILE)
IDLE_WATER = _knob("KG_IDLE_WATER", 1)          # insurance watering (shifts tomorrow's must-water to idle time today)
IDLE_WATER_PRIO = _knob("KG_IDLE_WATER_PRIO", 20)
SUNSET_DROP = _knob("KG_SUNSET_DROP", 0)        # bank carried goods before night so they sell today (h23) not at h0
SUNSET_MIN_VAL = _knob("KG_SUNSET_MIN_VAL", 200)
SELL_SAME_STEP = _knob("KG_SELL_SAME_STEP", 1)  # sell goods dropped this step in this step's market
DUTY = _knob("KG_DUTY", 0)                      # morning dispatch: one hand per far must-do cluster
DUTY_FAR_D = _knob("KG_DUTY_FAR_D", 6)          # shed distance from which a must-do tile counts as far
DUTY_RADIUS = _knob("KG_DUTY_RADIUS", 3)
DUTY_MAX = _knob("KG_DUTY_MAX", 3)              # max duty units per day
DUTY_BONUS = _knob("KG_DUTY_BONUS", 7000.0)     # > ON_TILE_BONUS + TIER_GAP so nothing peels the unit off
DUTY_HOUR = _knob("KG_DUTY_HOUR", 1)            # hands exist from hour 1
FAR_MUST = _knob("KG_FAR_MUST", 0)              # cancel most of the distance penalty for far must-do jobs
FAR_MUST_PRIO = _knob("KG_FAR_MUST_PRIO", 78)
FAR_MUST_D = _knob("KG_FAR_MUST_D", 6)
MATCH = _knob("KG_MATCH", 0)                    # Hungarian unit-job matching instead of greedy best-pair-first
HARVEST_HOUR = _knob("KG_HARVEST_HOUR", 0)      # before this hour premium harvests are low priority (0 = off)
HARVEST_EARLY_PRIO = _knob("KG_HARVEST_EARLY_PRIO", 50)
PICK_MIN = _knob("KG_PICK_MIN", 1)              # pick up at least this many feed/fert units per shed visit
PICK_MAX = _knob("KG_PICK_MAX", 5)
SELL_ORDER = _knob("KG_SELL_ORDER", 1)          # premium sells first in the order list (lockstep index race)
SELL_ORDER_PREM = _knob("KG_SELL_ORDER_PREM", 1.5)
SELL_STRAW_FIRST = _knob("KG_SELL_STRAW_FIRST", 0.0)   # extra stake weight for strawberry sells (index race)
NEAR_DROP = _knob("KG_NEAR_DROP", 0)            # drop goods when on/next to the shed
NEAR_DROP_VAL0 = _knob("KG_NEAR_DROP_VAL0", 100)
NEAR_DROP_VAL1 = _knob("KG_NEAR_DROP_VAL1", 300)
FLOOR_MILK_HI = _knob("KG_FLOOR_MILK_HI", 0.55)     # premium sell floor (x base) with drain >= 7
FLOOR_MILK_LO = _knob("KG_FLOOR_MILK_LO", 0.30)
FLOOR_STRAW_HI = _knob("KG_FLOOR_STRAW_HI", 0.50)
FLOOR_STRAW_LO = _knob("KG_FLOOR_STRAW_LO", 0.30)
# v18 sell timing (new meta 08-27: Milan-type agents sell strawberries/milk h17-23 spread, I dumped 80% at h0 via
# the night auto-drop -> $13/unit worse on equal volume).  Sell tonight instead of tomorrow morning:
EVENING_SELL_H = _knob("KG_EVENING_SELL_H", 0)     # from this hour the premium floor is scaled by EVENING_SELL_F (0 = off)
EVENING_SELL_F = _knob("KG_EVENING_SELL_F", 0.6)
ROUTE_SUNSET_H = _knob("KG_ROUTE_SUNSET_H", 0)     # from this hour units carrying >= ROUTE_SUNSET_MIN goods head home when the slack allows (0 = off)
ROUTE_SUNSET_MIN = _knob("KG_ROUTE_SUNSET_MIN", 2)
ANIMAL_SHEEP_FIRST = _knob("KG_ANIMAL_SHEEP_FIRST", 0)   # buy sheep before cows once a yarn store is open (tested 09-01: null)
MELON_NEAR_MAX = _knob("KG_MELON_NEAR_MAX", 0.0)   # >0: opening melons on NW tiles within this centre distance (0 = far corners, v12)
MELON_NEAR_MIN = _knob("KG_MELON_NEAR_MIN", 3.0)   # ...but not inside the animal zone
SHEEP_BEFORE_SEEDS = _knob("KG_SHEEP_BEFORE_SEEDS", 0)          # yarn draw: reserve cash for sheep before premium seeds (and buy sheep before cows)
SHEEP_BEFORE_SEEDS_LAST = _knob("KG_SHEEP_BEFORE_SEEDS_LAST", 10)
SHEEP_BEFORE_SEEDS_PER_DAY = _knob("KG_SHEEP_BEFORE_SEEDS_PER_DAY", 2)
FEED_RESERVE_DAYS = _knob("KG_FEED_RESERVE_DAYS", 3)   # days of feed wheat kept instead of sold (islet buys $4.9k of feed, I buy $11k: sell-then-rebuy churn)
FERT_SELL_MIN = _knob("KG_FERT_SELL_MIN", 12)
FERT_SELL_MAX = _knob("KG_FERT_SELL_MAX", 0)          # >0: max fertilizer units sold per step (pacing; 0 = sell all)
FERT_EARLY_SELL = _knob("KG_FERT_EARLY_SELL", 0)      # 1: sell fertilizer early (no hoarding for fertilizations) and buy back what the strawberries need
FERT_EARLY_LAST = _knob("KG_FERT_EARLY_LAST", 20)     # ...until this day
FERT_EARLY_MIN = _knob("KG_FERT_EARLY_MIN", 45)       # ...while the price is at least this
FERT_BUY_MAX = _knob("KG_FERT_BUY_MAX", 70)           # buy-back price cap
ANIMAL_RESERVE_SCALE = _knob("KG_ANIMAL_RESERVE_SCALE", 1.0)
ADAPT_COW_RAMP_MILK = [int(x) for x in str(_knob("KG_ADAPT_COW_RAMP_MILK", "")).replace(".", ",").split(",") if x]   # early cow ramp in milk draws (empty = default ramp)
ADAPT_COW_RAMP_MILK_MIN = _knob("KG_ADAPT_COW_RAMP_MILK_MIN", 2)   # milk shops needed
PREEMPT = _knob("KG_PREEMPT", 0)                     # 1: sell before the opponent's predicted dumps, hold after them
PREEMPT_HOLD = _knob("KG_PREEMPT_HOLD", 6)           # steps to hold a product after the opponent dumped it
PREEMPT_MIN_Q = _knob("KG_PREEMPT_MIN_Q", 3)         # opponent sale size that counts as a dump
ORACLE_TAPE = str(_knob("KG_ORACLE_TAPE", ""))       # instrument only: the opponent's replay (its future sells are known)
ORACLE_P = _knob("KG_ORACLE_P", 0)
MARKET_TAPE = _knob("KG_MARKET_TAPE", 0)             # 1: market orders + macro targets from the reference agent's nearest-state game
MLIB_PATH = str(_knob("KG_MLIB", "research/mlib_ziji.json"))
MTAPE_KEY_W = _knob("KG_MTAPE_KEY_W", 6.0)           # weight of a product-count mismatch vs one state unit
ADAPT_GEESE = _knob("KG_ADAPT_GEESE", 0)              # 1: geese with egg outlets (bakery / brunch)
GEESE_EGG1 = _knob("KG_GEESE_EGG1", 2)
GEESE_EGG2 = _knob("KG_GEESE_EGG2", 3)
GEESE_LAST = _knob("KG_GEESE_LAST", 20)
TAPE_OPENING = _knob("KG_TAPE_OPENING", 0)           # 1: replay a deterministic top agent's opening keyed by the shop draw
TAPE_OPENING_DAYS = _knob("KG_TAPE_OPENING_DAYS", 12) # hand over to the planner from this day (or earlier when the draw is uncovered)
TAPE_LIB_PATH = str(_knob("KG_TAPE_LIB", "research/tapes_ziji.json"))
TAPE_TREE = _knob("KG_TAPE_TREE", 0)                 # 1: splice-safe action-history tree replay (research/build_tree.py)
TAPE_TREE_PATH = str(_knob("KG_TAPE_TREE_LIB", "research/tree_zm.json"))
TAPE_TREE_MIN = _knob("KG_TAPE_TREE_MIN", 0.0)
TREE_FORCE_LEAF = str(_knob("KG_TREE_FORCE_LEAF", ""))  # instrument: follow the path to this leaf regardless of the town       # hand over to the planner when the best child's town match is below this
TAPE_NEAREST = _knob("KG_TAPE_NEAREST", 0)           # 1: when the actual draw is uncovered, keep following the same game tree
                                                      #    along the branch whose new shop is most similar (farm state stays identical)
WHEAT_FERT = _knob("KG_WHEAT_FERT", 0)                 # 1: fertilize wheat/carrots at the yield-window start (6 vs 4 wheat per tile)
WHEAT_FERT_LAST = _knob("KG_WHEAT_FERT_LAST", 24)
WHEAT_WIN_PRIO = _knob("KG_WHEAT_WIN_PRIO", 0)         # >0: priority of wheat waterings inside the yield window (default 74)
STRAW_TILE_HOLD = _knob("KG_STRAW_TILE_HOLD", 0)      # 1: keep STRAW-role tiles empty for the strawberry seed instead of replanting wheat
ZONE_SKIP_TOMATO = _knob("KG_ZONE_SKIP_TOMATO", 0)     # 1: the animal zone never claims a growing tomato
ANIMALS_FIRST = _knob("KG_ANIMALS_FIRST", 0)         # 1: buy animals before hires/land/seeds until ANIMALS_FIRST_LAST
ANIMALS_FIRST_LAST = _knob("KG_ANIMALS_FIRST_LAST", 10)
COWS_BEFORE_SEEDS = _knob("KG_COWS_BEFORE_SEEDS", 0)             # milk draw: reserve cash for cows ahead of premium seeds
COWS_BEFORE_SEEDS_LAST = _knob("KG_COWS_BEFORE_SEEDS_LAST", 10)
COWS_BEFORE_SEEDS_PER_DAY = _knob("KG_COWS_BEFORE_SEEDS_PER_DAY", 3)
COWS_BEFORE_SEEDS_MIN = _knob("KG_COWS_BEFORE_SEEDS_MIN", 2)     # milk shops needed   # cash reserve multiplier when buying animals (the 09-07 leaders hold 6 cows by day 6 with $594 left)          # fertilizer sell floor (no town drain; islet averages $42/unit vs my $34)
WOOL_HOLD = _knob("KG_WOOL_HOLD", 1)            # hold glutted wool for a future yarn store
WOOL_HOLD_PRICE = _knob("KG_WOOL_HOLD_PRICE", 60)
WOOL_HOLD_LAST = _knob("KG_WOOL_HOLD_LAST", 24)
WOOL_HOLD_SLACK = _knob("KG_WOOL_HOLD_SLACK", 0)  # daytime overflow threshold rises with held wool (0 = off)
WHEAT_LAST_PLANT = _knob("KG_WHEAT_LAST", 26)
BULK_DROP = _knob("KG_BULK_DROP", 0)
OVERFLOW_SELL = _knob("KG_OVERFLOW", 1)
OVERFLOW_WHEAT_FIRST = _knob("KG_OVERFLOW_WHEAT_FIRST", 0)   # overflow: sell wheat/fert reserve before premium goods
WALK_COST = 25.0
STICK_BONUS = 60.0
ON_TILE_BONUS = _knob("KG_ON_TILE", 3500.0)
TIER_GAP = 3000.0
PRIO_W = 8.0


def job_tier(prio):
    if prio >= 90:
        return 3
    if prio >= 60:
        return 2
    return 1


ANCHOR_W = _knob("KG_ANCHOR_W", 4.0)


def compute_anchors(W, n_hands):
    """Split the worked tiles into angular sectors around the shed and give
    each hired hand the centroid of one sector as its territory anchor.
    (Hand indices are 1..n_hands; the farmer stays a free agent.)"""
    anchors = {}
    if n_hands <= 0 or ANCHOR_W <= 0:
        return anchors
    pts = []
    for y in range(BOARD):
        for x in range(BOARD):
            tl = W.tiles[y][x]
            if tl == "LOCKED":
                continue
            # tiles that will need visits: plants and animals; empty tiles a bit
            w = 1.0
            if tl is None:
                w = 0.35
            pts.append((math.atan2(y - CENTER[1], x - CENTER[0]), x, y, w))
    if not pts:
        return anchors
    pts.sort()
    total_w = sum(p[3] for p in pts)
    per = total_w / n_hands
    acc = 0.0
    grp = []
    gi = 1
    for p in pts:
        grp.append(p)
        acc += p[3]
        if acc >= per * gi and gi <= n_hands:
            sx = sum(q[1] * q[3] for q in grp) / max(1e-9, sum(q[3] for q in grp))
            sy = sum(q[2] * q[3] for q in grp) / max(1e-9, sum(q[3] for q in grp))
            anchors[gi] = (sx, sy)
            gi += 1
            grp = []
    if grp and gi <= n_hands:
        sx = sum(q[1] * q[3] for q in grp) / max(1e-9, sum(q[3] for q in grp))
        sy = sum(q[2] * q[3] for q in grp) / max(1e-9, sum(q[3] for q in grp))
        anchors[gi] = (sx, sy)
    return anchors


SECTORS = _knob("KG_SECTORS", 0)                # v13: sticky day-level sector ownership (crop sectors + animal keepers)
SECT_KEEPERS = _knob("KG_SECT_KEEPERS", 2)      # hands dedicated to the animal zone
SECT_URGENT_D = _knob("KG_SECT_URGENT_D", 3)    # tier-3 jobs within this distance may be taken by anyone
SECT_HELP = _knob("KG_SECT_HELP", 1)            # a hand with nothing left in its sector helps neighbours
SECT_PICK = _knob("KG_SECT_PICK", 8)            # keepers pick up feed for their whole sector (cap)
SECT_PER_KEEPER = _knob("KG_SECT_PER_KEEPER", 4)  # animals per keeper
SECT_HANDS_PER = _knob("KG_SECT_HANDS_PER", 1)  # hands sharing one crop sector
SECT_HELP_MAX = _knob("KG_SECT_HELP_MAX", 0)    # help neighbours when own sector has <= this many jobs left
SECT_CROP_FREE = _knob("KG_SECT_CROP_FREE", 0)  # crop hands unrestricted among crop tiles (only keepers are dedicated)
SECT_PRIO_W = _knob("KG_SECT_PRIO_W", 8.0)      # priority weight for crop hands inside their sector (sweep if small)
SECT_W_STRAW = _knob("KG_SECT_W_STRAW", 2.0)
SECT_W_ANIMAL = _knob("KG_SECT_W_ANIMAL", 3.0)
TERRITORY = _knob("KG_TERRITORY", 0)
TERR_BONUS = _knob("KG_TERR_BONUS", 600.0)
TERR_KEEPERS = _knob("KG_TERR_KEEPERS", 2)
TERR_REPLAN_HOURS = (1, 12)


def compute_sectors(st, W, n_hands):
    """Day-level partition of the farm among hired hands (v13).
    Animal structures -> SECT_KEEPERS keepers (angular halves of the zone);
    every other unlocked tile -> one of the remaining hands, contiguous angular
    arcs balanced by expected daily workload.  Returns (tile->sector, unit->sector).
    Sector ids: 'A0'.. for keepers, 'C0'.. for crop sectors."""
    tile_sec = {}
    unit_sec = {}
    if n_hands <= 0:
        return tile_sec, unit_sec
    animal_tiles = []
    crop_tiles = []
    for y in range(BOARD):
        for x in range(BOARD):
            tl = W.tiles[y][x]
            if tl == "LOCKED":
                continue
            role = st.roles.get((x, y))
            if is_struct(tl) or has_animal(tl) or role == "ANIMAL":
                animal_tiles.append(((x, y), SECT_W_ANIMAL if has_animal(tl) else 0.5))
                continue
            w = 0.4
            if is_plant(tl):
                crop = tl.get("crop")
                w = SECT_W_STRAW if crop == "STRAWBERRY" else (1.5 if crop == "MELON" else 1.0)
            elif role in ("STRAW", "WHEAT", "MELON", "CARROT"):
                w = 0.6
            crop_tiles.append(((x, y), w))
    n_anim = sum(1 for t, w in animal_tiles if w >= SECT_W_ANIMAL)
    k_keep = max(SECT_KEEPERS, int(math.ceil(n_anim / float(SECT_PER_KEEPER)))) if animal_tiles else 0
    k_keep = min(k_keep, max(0, n_hands - 3))
    k_crop = n_hands - k_keep
    def ang(t):
        return math.atan2(t[0][1] - CENTER[1], t[0][0] - CENTER[0])
    def split(items, k, prefix, first_unit):
        if k <= 0 or not items:
            return
        items = sorted(items, key=ang)
        total = sum(w for _, w in items)
        per = total / k
        acc = 0.0
        gi = 0
        for t, w in items:
            tile_sec[t] = f"{prefix}{min(gi, k - 1)}"
            acc += w
            if acc >= per * (gi + 1) and gi < k - 1:
                gi += 1
        for g in range(k):
            unit_sec[first_unit + g] = f"{prefix}{g}"
    split(animal_tiles, k_keep, "A", 1)
    # crop sectors may be shared by SECT_HANDS_PER hands (fewer, larger arcs)
    n_sec = max(1, int(math.ceil(k_crop / float(max(1, SECT_HANDS_PER))))) if k_crop > 0 else 0
    if n_sec > 0:
        items = sorted(crop_tiles, key=ang)
        total = sum(w for _, w in items)
        per = total / n_sec
        acc = 0.0
        gi = 0
        for t, w in items:
            tile_sec[t] = f"C{min(gi, n_sec - 1)}"
            acc += w
            if acc >= per * (gi + 1) and gi < n_sec - 1:
                gi += 1
        for h in range(k_crop):
            unit_sec[1 + k_keep + h] = f"C{h % n_sec}"
    return tile_sec, unit_sec


def compute_territory(st, W, jobs):
    """Hard partition of today's work among hired hands.
    - the animal zone (structures) goes to TERR_KEEPERS hands (angular halves)
    - the remaining job tiles are split into contiguous angular arcs, balanced
      by number of actions, one arc per remaining hand.
    Farmer (unit 0) is a free agent.  Returns {(x,y): unit_idx}."""
    n_hands = len(W.units) - 1
    terr = {}
    if n_hands <= 0:
        return terr
    animal_tiles = []
    crop_jobs = []
    for j in jobs:
        x, y = j.tile
        tl = W.tiles[y][x]
        if isinstance(tl, dict) and ("animal" in tl or tl.get("kind") in ("COOP", "PASTURE")):
            animal_tiles.append((j.tile, len(j.actions)))
        else:
            crop_jobs.append((j.tile, len(j.actions)))
    k_keep = min(TERR_KEEPERS, n_hands) if animal_tiles else 0
    # keepers get hands 1..k_keep, crops the rest
    def ang(t):
        return math.atan2(t[0][1] - CENTER[1], t[0][0] - CENTER[0])
    def split(items, k, first_unit):
        if k <= 0 or not items:
            return
        items = sorted(items, key=ang)
        total = sum(w for _, w in items)
        per = total / k
        acc = 0.0
        gi = 0
        for t, w in items:
            terr[t] = first_unit + min(gi, k - 1)
            acc += w
            if acc >= per * (gi + 1) and gi < k - 1:
                gi += 1
    split(animal_tiles, k_keep, 1)
    split(crop_jobs, n_hands - k_keep, 1 + k_keep)
    return terr


def _match_pairs(pairs, n_units):
    """Hungarian assignment on the (unit, job) score matrix; returns the chosen
    pairs (score desc) followed by the remaining pairs (fallback ordering)."""
    units = sorted(set(p[1] for p in pairs))
    jobs = sorted(set(p[2] for p in pairs))
    if not units or not jobs:
        return pairs
    ui = {u: k for k, u in enumerate(units)}
    ji = {j: k for k, j in enumerate(jobs)}
    best = {}
    for p in pairs:
        key = (p[1], p[2])
        if key not in best or p[0] > best[key][0]:
            best[key] = p
    n = max(len(units), len(jobs))
    BIG = 10 ** 7
    # cost matrix (minimise): cost = BIG - score for feasible pairs, 2*BIG otherwise
    cost = [[2 * BIG] * n for _ in range(n)]
    for (u, j), p in best.items():
        cost[ui[u]][ji[j]] = BIG - p[0]
    # Hungarian (Kuhn-Munkres), O(n^3), n <= ~80
    INF = float("inf")
    u_pot = [0.0] * (n + 1); v_pot = [0.0] * (n + 1); p_match = [0] * (n + 1); way = [0] * (n + 1)
    for i in range(1, n + 1):
        p_match[0] = i
        j0 = 0
        minv = [INF] * (n + 1)
        used = [False] * (n + 1)
        while True:
            used[j0] = True
            i0 = p_match[j0]; delta = INF; j1 = 0
            for j in range(1, n + 1):
                if not used[j]:
                    cur = cost[i0 - 1][j - 1] - u_pot[i0] - v_pot[j]
                    if cur < minv[j]:
                        minv[j] = cur; way[j] = j0
                    if minv[j] < delta:
                        delta = minv[j]; j1 = j
            for j in range(n + 1):
                if used[j]:
                    u_pot[p_match[j]] += delta; v_pot[j] -= delta
                else:
                    minv[j] -= delta
            j0 = j1
            if p_match[j0] == 0:
                break
        while True:
            j1 = way[j0]
            p_match[j0] = p_match[j1]
            j0 = j1
            if j0 == 0:
                break
    chosen = []
    for j in range(1, n + 1):
        i = p_match[j]
        if i and i - 1 < len(units) and j - 1 < len(jobs):
            key = (units[i - 1], jobs[j - 1])
            if key in best:
                chosen.append(best[key])
    chosen.sort(key=lambda p: -p[0])
    chosen_keys = set((p[1], p[2]) for p in chosen)
    rest = [p for p in pairs if (p[1], p[2]) not in chosen_keys]
    return chosen + rest


def _act_ok(a, iv):
    if a == "FERTILIZE":
        return int(iv.get("FERTILIZER", 0)) > 0
    if a == "FEED":
        return int(iv.get("WHEAT", 0)) > 0
    return True


def _partial_ok(job, iv):
    """Can a unit without the needed item still do something useful at the tile?"""
    if job.actions[0] in ("PLACE", "PLANT"):
        return False
    return any(_act_ok(a, iv) for a in job.actions)


def assign(st, W, jobs):
    """Return list of unit actions (index aligned with W.units)."""
    n_units = len(W.units)
    actions = [["PASS"] for _ in range(n_units)]
    day, hour = W.day, W.hour
    if TERRITORY and (hour in TERR_REPLAN_HOURS or st.terr_hour < 0 or len(W.units) - 1 != st.terr_hands):
        st.territory = compute_territory(st, W, jobs)
        st.terr_hour = hour
        st.terr_hands = len(W.units) - 1
    endgame = day >= LAST_DAY
    turns_left_today = TPD - 1 - hour            # turns after this one today
    shed = dict(W.shed)
    shed_room = SHED_CAP - shed_total(W)

    # per-unit inventories (mutable copies for reservation)
    invs = [dict(iv) for iv in W.invs]

    # ---- special: drop jobs for units carrying valuable stuff or endgame ----
    unit_forced = {}
    for i, pos in enumerate(W.units):
        iv = invs[i]
        val = 0.0
        cnt = 0
        for it, n in iv.items():
            if it in MARKET_PARAMS and n > 0:
                val += n * W.prices.get(it, MARKET_PARAMS[it]["base"])
                cnt += n
        sd, dsh = nearest_shed(pos)
        if cnt == 0:
            continue
        must_drop = False
        if endgame:
            # must reach the shed and drop before the last executed step
            if dsh >= W.steps_left - 1:
                must_drop = True
        melons = iv.get("MELON", 0)
        if melons >= 6:
            must_drop = True
        # premium goods: bank them promptly so they are sold today (ahead of
        # opponents who sell the next morning)
        prem_val = sum(iv.get(it, 0) * W.prices.get(it, MARKET_PARAMS[it]["base"]) for it in PREMIUM)
        if prem_val >= 300 * PREM_DROP_SCALE and dsh <= 2:
            must_drop = True
        if prem_val >= 900 * PREM_DROP_SCALE and dsh <= 4:
            must_drop = True
        if prem_val >= 2000 * PREM_DROP_SCALE and dsh <= 7:
            must_drop = True
        # bulk: do not walk around with a big load (it all lands in the shed at
        # night and overflow is destroyed; selling earlier is better anyway)
        if BULK_DROP:
            if cnt >= 8 and dsh <= 2:
                must_drop = True
            if cnt >= 14 and dsh <= 4:
                must_drop = True
            if hour >= 20 and cnt >= 4 and dsh <= 3:
                must_drop = True
        if hour >= 22 and dsh <= 1 and shed_room > cnt:
            must_drop = True
        # standing on / next to the shed with goods: bank them now (sold this
        # step) instead of carrying them around until the night auto-drop
        if NEAR_DROP and shed_room > cnt and ((dsh == 0 and prem_val >= NEAR_DROP_VAL0) or (dsh == 1 and prem_val >= NEAR_DROP_VAL1)):
            must_drop = True
        # sunset: goods still carried at night are auto-dropped and sold at h0
        # together with the opponent's; walking back now sells them this evening,
        # ahead of that batch.  Start when the shed is just reachable by h23.
        if SUNSET_DROP and not endgame and val >= SUNSET_MIN_VAL and hour + dsh >= 23 and hour + dsh <= 23 + 0 and shed_room > cnt:
            must_drop = True
        if must_drop:
            unit_forced[i] = ("DROP", sd)

    # ---- item logistics: how many jobs need each item, how much is carried ----
    need_jobs = {}
    for job in jobs:
        if job.need is not None:
            need_jobs[job.need] = need_jobs.get(job.need, 0) + 1
    carried = {}
    carriers = {}
    for i, iv in enumerate(invs):
        for it, n in iv.items():
            if n > 0:
                carried[it] = carried.get(it, 0) + n
                carriers[it] = carriers.get(it, 0) + 1

    # ---- last call: important jobs that must be started now (by the nearest
    # free unit) to still be completed today are lifted to the top tier ----
    last_call = set()
    if LAST_CALL and not endgame:
        free_units = [pos for i, pos in enumerate(W.units) if i not in unit_forced]
        for j, job in enumerate(jobs):
            if job.prio < LAST_CALL_PRIO or job_tier(job.prio) >= 3 or not free_units:
                continue
            dmin = min(dist(pos, job.tile) for pos in free_units)
            if dmin + len(job.actions) >= turns_left_today - LAST_CALL_SLACK:
                last_call.add(j)

    # ---- v13 sectors: which hands have nothing left in their own sector ----
    sec_idle = set()
    if SECTORS and st.tile_sec:
        sec_jobs = {}
        for job in jobs:
            sec_jobs[st.tile_sec.get(job.tile)] = sec_jobs.get(st.tile_sec.get(job.tile), 0) + 1
        for i in range(1, len(W.units)):
            if sec_jobs.get(st.unit_sec.get(i), 0) <= SECT_HELP_MAX:
                sec_idle.add(i)
        # sectors were computed for hires_wanted hands; if fewer/more hands exist, map extras
        for i in range(1, len(W.units)):
            if i not in st.unit_sec:
                st.unit_sec[i] = st.unit_sec.get(((i - 1) % max(1, len(st.unit_sec))) + 1) if st.unit_sec else None
    # ---- build candidate (unit, job) scores ----
    pairs = []
    for i, pos in enumerate(W.units):
        if i in unit_forced:
            continue
        iv = invs[i]
        stick = st.targets.get(i)
        my_sec = st.unit_sec.get(i) if SECTORS else None
        for j, job in enumerate(jobs):
            d = dist(pos, job.tile)
            if SECTORS and i > 0 and st.tile_sec:
                jsec = st.tile_sec.get(job.tile)
                if jsec is not None and my_sec is not None and jsec != my_sec and not (
                        SECT_CROP_FREE and jsec[0] == "C" and my_sec[0] == "C"):
                    urgent = job_tier(job.prio) >= 3 and d <= SECT_URGENT_D
                    if not urgent and j not in last_call:
                        # crop hands may help a neighbouring crop sector when their own is quiet
                        adjacent = (my_sec[0] == "C" and jsec[0] == "C" and abs(int(jsec[1:]) - int(my_sec[1:])) <= 1)
                        if not (SECT_HELP and adjacent and i in sec_idle):
                            continue
            extra = 0
            carrier_bonus = 0
            partial = False
            if job.need is not None:
                if iv.get(job.need, 0) > 0:
                    carrier_bonus = 45
                else:
                    # need to fetch from shed first
                    if shed.get(job.need, 0) <= 0:
                        if PARTIAL_NEED and job.need == "FERTILIZER" and _partial_ok(job, iv):
                            partial = True
                            carrier_bonus = -PARTIAL_PEN
                        else:
                            continue
                    else:
                        sd, dsh = nearest_shed(pos)
                        extra = dsh + dist(sd, job.tile) - d
            total_d = d + extra
            if endgame:
                # must be able to do the job, get back to the shed and drop
                _, dback = nearest_shed(job.tile)
                if total_d + len(job.actions) + dback + 1 > W.steps_left:
                    continue
            else:
                if total_d + len(job.actions) > turns_left_today + 1 and job.prio < 100:
                    continue
            tier = job_tier(job.prio)
            pw = PRIO_W
            if SECTORS and i > 0 and my_sec is not None and my_sec[0] == "C" and tier < 3:
                pw = SECT_PRIO_W
            score = tier * TIER_GAP + job.prio * pw - total_d * WALK_COST + carrier_bonus
            if j in last_call:
                score += (3 - tier) * TIER_GAP + LAST_CALL_EXTRA
            if DUTY and st.duty and job.tile in st.duty.get(i, ()):
                score += DUTY_BONUS
            if FAR_MUST and job.prio >= FAR_MUST_PRIO and total_d >= FAR_MUST_D and job.kind != "animal":
                # a must-do job on a far tile is starved all day by nearer jobs of
                # the same tier (the greedy re-plans hourly); pay the walk once
                score += WALK_COST * (total_d - FAR_MUST_D + 1)
            if stick == job.tile:
                score += STICK_BONUS
            anc = st.anchors.get(i)
            if anc is not None and job.kind != "animal":
                score -= ANCHOR_W * (abs(job.tile[0] - anc[0]) + abs(job.tile[1] - anc[1]))
            if TERRITORY and i > 0 and job_tier(job.prio) < 3:
                owner = st.territory.get(job.tile)
                if owner is not None and owner != i:
                    score -= TERR_BONUS       # someone else's tile (unless urgent)
            if d == 0 and extra == 0:
                # finish the work on the tile we stand on: an action here costs
                # no walking, so it is nearly always the best use of the turn
                score += ON_TILE_BONUS
            score += 6 * len(job.actions)
            pairs.append((score, i, j, extra, partial))
    pairs.sort(key=lambda p: -p[0])
    if MATCH and pairs:
        # optimal one-to-one assignment (maximise the sum of scores) instead of
        # the greedy best-pair-first: the greedy sends a unit far for a high
        # score while a nearer unit takes something else -> longer routes
        pairs = _match_pairs(pairs, n_units)

    unit_done = set(unit_forced.keys())
    job_taken = set()
    new_targets = {}
    fetch_reserved = {}     # item -> count reserved for pickup this turn
    for score, i, j, extra, partial in pairs:
        if i in unit_done or j in job_taken:
            continue
        job = jobs[j]
        pos = W.units[i]
        iv = invs[i]
        job_taken.add(j)
        unit_done.add(i)
        new_targets[i] = job.tile
        # need item?
        if job.need is not None and iv.get(job.need, 0) <= 0 and not partial:
            sd, dsh = nearest_shed(pos)
            if dsh == 0:
                # pick up now
                avail = shed.get(job.need, 0) - fetch_reserved.get(job.need, 0)
                if avail <= 0:
                    unit_done.discard(i)
                    job_taken.discard(j)
                    continue
                k = 1
                if job.need in ("WHEAT", "FERTILIZER"):
                    # jobs still needing this item that no carried stock covers
                    remaining = sum(1 for jj, jb in enumerate(jobs) if jb.need == job.need and jj not in job_taken) + 1
                    uncovered = max(1, remaining - carried.get(job.need, 0))
                    k = max(1, min(avail, max(uncovered, min(PICK_MIN, remaining)), PICK_MAX))
                    if SECTORS and job.need == "WHEAT" and (st.unit_sec.get(i) or "").startswith("A"):
                        mine = sum(1 for jj, jb in enumerate(jobs) if jb.need == "WHEAT" and jj not in job_taken
                                   and st.tile_sec.get(jb.tile) == st.unit_sec.get(i))
                        k = max(1, min(avail, mine, SECT_PICK))
                    carried[job.need] = carried.get(job.need, 0) + k
                fetch_reserved[job.need] = fetch_reserved.get(job.need, 0) + k
                actions[i] = ["PICKUP", job.need, k]
            else:
                actions[i] = [step_toward(pos, sd)]
            continue
        if pos == job.tile:
            a0 = job.actions[0]
            if a0 == "PLACE":
                actions[i] = ["PLACE", job.actions[1]]
            elif a0 == "PLANT":
                actions[i] = ["PLANT", job.actions[1]]
            else:
                if partial:
                    a0 = next((a for a in job.actions if _act_ok(a, iv)), a0)
                actions[i] = [a0]
        else:
            actions[i] = [step_toward(pos, job.tile)]

    for i, (kind, sd) in unit_forced.items():
        pos = W.units[i]
        if pos == sd or pos in SHED_TILES:
            actions[i] = ["DROP"]
        else:
            actions[i] = [step_toward(pos, sd)]
        new_targets[i] = sd

    # idle units: on the last day walk to shed; otherwise drift toward shed a bit
    for i, pos in enumerate(W.units):
        if i in unit_done:
            continue
        iv = invs[i]
        cnt = sum(v for k, v in iv.items() if k in MARKET_PARAMS)
        sd, dsh = nearest_shed(pos)
        if cnt > 0:
            actions[i] = ["DROP"] if dsh == 0 else [step_toward(pos, sd)]
            new_targets[i] = sd
        elif dsh > 1:
            # drift back toward the centre so the next job is close
            actions[i] = [step_toward(pos, sd)]
        else:
            actions[i] = ["PASS"]
    st.targets = new_targets
    return actions


# ----------------------------------------------------------------------------
# v13: day-level route planner (KG_ROUTE=1; default OFF == v12 behaviour)
# ----------------------------------------------------------------------------
# Every unit gets a route (ordered stops) for the whole day at hour 0 (hands
# that are still to be hired get virtual routes and pick them up when they
# spawn).  Routes are built by an angular sweep around the shed (each unit =
# one contiguous wedge: shed -> its 1-2 animals -> strawberry ring -> outer
# wheat), ordered nearest-neighbour + 2-opt, with one bulk PICKUP of the wheat
# / fertilizer the route needs.  Must-do jobs (class A) are guaranteed inside
# the time budget; lower classes fill the remaining time.  During the day the
# executor follows the routes; jobs that appear later go through a pool and
# are inserted where cheapest.  Premium goods are dropped when passing the shed
# (and with the v12 value/distance rule, so the front-running is unchanged).
ROUTE = _knob("KG_ROUTE", 0)
ROUTE_SLACK = _knob("KG_ROUTE_SLACK", 2)          # planning slack (turns) per route
ROUTE_MUST = _knob("KG_ROUTE_MUST", 76)           # prio >= this = class A (must be done today)
ROUTE_PASS_VAL = _knob("KG_ROUTE_PASS_VAL", 300.0)  # premium value banked when standing on a shed tile
ROUTE_BAL = _knob("KG_ROUTE_BAL", 1.15)           # sweep target fill = mean load * BAL
ROUTE_IDLE_D = _knob("KG_ROUTE_IDLE_D", 5)
ROUTE_ANIMAL_B = _knob("KG_ROUTE_ANIMAL_B", 0)
ROUTE_PLAN_C_MAX = _knob("KG_ROUTE_PLAN_C_MAX", 60)   # class-C insertions tried at h0 (time cap)
ROUTE_PLAN_WORK_MAX = _knob("KG_ROUTE_PLAN_WORK_MAX", 200)
ROUTE_MILK_BANK = _knob("KG_ROUTE_MILK_BANK", 0)     # bank >= this many milk within 2 tiles in the morning (0 = off)
ROUTE_MILK_BANK_H = _knob("KG_ROUTE_MILK_BANK_H", 12)      # animal leftovers (collect/harvest) at least class B        # idle units take low-class pool jobs within this distance
DEBUG = _knob("KG_DEBUG", 0)
ROUTE_BANK_FLOOR = _knob("KG_ROUTE_BANK_FLOOR", 1)   # bank only goods that sell now (price >= floor)


class Stop:
    __slots__ = ("tile", "kind", "key", "acts", "need", "cls", "prio", "t", "item", "k")

    def __init__(self, tile, kind, key=None, acts=(), need=None, cls=2, prio=0, t=1, item=None, k=0):
        self.tile = tile
        self.kind = kind          # job kind, or "PICKUP" / "DROP"
        self.key = key
        self.acts = list(acts)
        self.need = need
        self.cls = cls            # 0 = A (must), 1 = B, 2 = C
        self.prio = prio
        self.t = t                # estimated turns on the tile
        self.item = item          # PICKUP item
        self.k = k                # PICKUP count


def _job_cls(job, W=None):
    if job.kind == "place":
        return 0
    prio = job.prio
    if W is not None and "WATER" in job.actions and prio < ROUTE_MUST:
        # a plant with consecutive_unwatered >= 1 (incl. every plant on its
        # planting day) dies tonight without water: always class A
        tl = tile_at(W, job.tile)
        if isinstance(tl, dict) and int(tl.get("consecutive_unwatered", 0)) >= 1:
            prio = ROUTE_MUST
    if W is not None and prio >= 90 and job.actions == ["WATER"] and W.hour >= URGENT_HOUR:
        # the hourly escalation (URGENT_HOUR -> 92) also lifts plain waters:
        # classify by what the water is worth, not by the escalated number
        tl = tile_at(W, job.tile)
        if isinstance(tl, dict):
            cu = int(tl.get("consecutive_unwatered", 0))
            age = W.day - int(tl.get("planted_day", W.day))
            crop = tl.get("crop")
            if not (cu >= 1 or (crop == "STRAWBERRY" and age in (9, 11, 13, 15))
                    or (crop == "MELON" and age >= 6) or age == 0):
                prio = 74
    if job.kind == "plant" and len(job.actions) > 1 and job.actions[1] in ("STRAWBERRY", "MELON", "TOMATO"):
        return 0
    if prio >= ROUTE_MUST:
        return 0
    if prio >= 66:
        return 1
    if job.kind == "animal" and ROUTE_ANIMAL_B:
        # a leftover COLLECT / HARVEST on an animal (prio 62-68) is still worth a
        # short walk: fertilizer collections are 8-10% below the top agents
        return 1
    return 2


def _job_time(job, W):
    """Estimated turns spent on the tile (job actions + the usual follow-ups)."""
    t = len(job.actions)
    if job.kind == "plant":
        return t + 1                    # the new plant must be watered the same day
    if W.day <= WHEAT_LAST_PLANT:
        tl = tile_at(W, job.tile)
        crop = tl.get("crop") if isinstance(tl, dict) else None
        if "HARVEST" in job.actions and crop in ("WHEAT", "CARROT", "MELON"):
            t += 2                      # replant + first water on the freed tile
        elif "DIG" in job.actions:
            t += 2
    return t


def _stop_from_job(job, W):
    return Stop(job.tile, job.kind, key=job.key, acts=job.actions, need=job.need,
                cls=_job_cls(job, W), prio=job.prio, t=_job_time(job, W))


def _walk(a, b):
    """Manhattan distance; None = 'a shed access tile' (any of the four)."""
    if a is None and b is None:
        return 0
    if a is None:
        return min(dist(s, b) for s in SHED_TILES)
    if b is None:
        return min(dist(a, s) for s in SHED_TILES)
    return dist(a, b)


def _stop_pos(s):
    return None if s.kind in ("PICKUP", "DROP") else s.tile


def _route_time(route, start):
    """Turns to execute the route from position start (None = shed)."""
    t = 0
    cur = start
    for s in route:
        p = _stop_pos(s)
        t += _walk(cur, p) + s.t
        cur = p
    return t


def _order_stops(stops):
    """Nearest-neighbour path from the shed, improved by 2-opt (open path)."""
    if len(stops) <= 1:
        return list(stops)
    rem = list(stops)
    path = []
    cur = None
    while rem:
        best = min(rem, key=lambda s: (_walk(cur, s.tile), -s.prio, s.tile))
        path.append(best)
        rem.remove(best)
        cur = best.tile
    n = len(path)
    if n <= 2:
        return path
    improved = True
    guard = 0
    while improved and guard < 20:
        improved = False
        guard += 1
        for i in range(-1, n - 2):
            a = path[i].tile if i >= 0 else None
            for k in range(i + 2, n):
                b = path[i + 1].tile
                c = path[k].tile
                d = path[k + 1].tile if k + 1 < n else None
                before = _walk(a, b) + (dist(c, d) if d is not None else 0)
                after = _walk(a, c) + (dist(b, d) if d is not None else 0)
                if after < before:
                    path[i + 1:k + 1] = path[i + 1:k + 1][::-1]
                    improved = True
    return path


def _premium_value(iv, W):
    """Value of carried premium goods that would sell right now (price at or
    above the selling floor); goods below the floor would only sit in the shed
    (and get dumped by the overflow rule), so carrying them until night is free."""
    v = 0.0
    for it in PREMIUM:
        n = int(iv.get(it, 0))
        if n <= 0:
            continue
        pr = W.prices.get(it, MARKET_PARAMS[it]["base"])
        if ROUTE_BANK_FLOOR and it != "MELON" and pr < premium_floor(it, W):
            continue
        v += n * pr
    return v


def _v12_must_drop(iv, W, dsh, hour, shed_room):
    """The v12 banking rule (value / distance thresholds), minus the endgame part."""
    cnt = sum(int(n) for it, n in iv.items() if it in MARKET_PARAMS and n > 0)
    if cnt == 0:
        return False
    if int(iv.get("MELON", 0)) >= 6:
        return True
    prem_val = _premium_value(iv, W)
    if prem_val >= 300 * PREM_DROP_SCALE and dsh <= 2:
        return True
    if prem_val >= 900 * PREM_DROP_SCALE and dsh <= 4:
        return True
    if prem_val >= 2000 * PREM_DROP_SCALE and dsh <= 7:
        return True
    if ROUTE_MILK_BANK and hour <= ROUTE_MILK_BANK_H and dsh <= 2:
        # morning milk: the first seller of the day gets the price (the top
        # agents dump at h3-h9); bank even a small load while near the shed
        n_milk = int(iv.get("MILK", 0))
        if n_milk >= ROUTE_MILK_BANK and n_milk * W.prices.get("MILK", 160) >= premium_floor("MILK", W) * n_milk:
            return True
    return False


def _bank_action(iv, route, W, shed_room):
    """DROP everything, or PLACE only the most valuable premium item when the
    unit still needs something it carries (wheat / fertilizer / an animal)."""
    keeps = set()
    for s in route:
        if s.need is not None:
            keeps.add(s.need)
        if s.kind == "PICKUP" and s.item:
            keeps.add(s.item)
    if not any(int(iv.get(k, 0)) > 0 for k in keeps):
        return ["DROP"]
    best_it = max(PREMIUM, key=lambda it: int(iv.get(it, 0)) * W.prices.get(it, MARKET_PARAMS[it]["base"]))
    n = int(iv.get(best_it, 0))
    if n <= 0:
        return None
    return ["PLACE", best_it, max(1, min(n, shed_room))]


def _plan_pickups(route, W, shed_left):
    """Prepend PICKUP stops for the wheat / fertilizer the route needs."""
    n_feed = sum(1 for s in route if s.need == "WHEAT")
    need_f = 0
    running = 0     # fertilizer collected on the way minus used
    for s in route:
        if s.need == "FERTILIZER" and "FERTILIZE" in s.acts:
            if running > 0:
                running -= 1
            else:
                need_f += 1
        if "COLLECT_FERTILIZER" in s.acts:
            running += 1
    pre = []
    if n_feed > 0:
        # the h0 market buys what is short; ask for the full need
        pre.append(Stop(None, "PICKUP", key=("PICKUP", "WHEAT"), item="WHEAT", k=n_feed, cls=0, t=1))
    if need_f > 0:
        k = min(need_f, max(0, shed_left.get("FERTILIZER", 0)))
        if k > 0:
            pre.append(Stop(None, "PICKUP", key=("PICKUP", "FERTILIZER"), item="FERTILIZER", k=k, cls=0, t=1))
            shed_left["FERTILIZER"] = shed_left.get("FERTILIZER", 0) - k
    return pre + route


def _plan_drops(route, W, budget):
    """Insert DROP stops where the v12 banking rule would send the unit back."""
    out = []
    iv = {}
    cur = None
    total = _route_time(route, None)
    for s in route:
        p = _stop_pos(s)
        out.append(s)
        if p is not None:
            tl = tile_at(W, p)
            if isinstance(tl, dict) and "HARVEST" in s.acts:
                yu = int(tl.get("yield_units", 0))
                if "WATER" in s.acts and tl.get("kind") == "PLANT" and not CROPS.get(tl.get("crop"), {}).get("ongoing", True):
                    yu += 1                       # the water before the harvest adds a unit
                if has_animal(tl):
                    prod = ANIMALS[tl["animal"]]["product"]
                    iv[prod] = iv.get(prod, 0) + yu
                elif tl.get("crop"):
                    iv[tl["crop"]] = iv.get(tl["crop"], 0) + yu
        cur = p
        if p is not None and _v12_must_drop(iv, W, _walk(p, None), 12, 100):
            extra = 2 * _walk(p, None) + 1          # there and back (approx.)
            if int(iv.get("MELON", 0)) >= 6 or total + extra <= budget:
                out.append(Stop(None, "DROP", key=("DROP", len(out)), cls=1, t=1))
                total += extra
                iv = {}
                cur = None
    return out


def _a_rank(s, W):
    """Value order among class-A stops (higher = keep first when the day is
    overloaded): dying plants > melon dump > feed > place > fertilize > ..."""
    if s.cls != 0:
        return -1
    if s.kind == "harvest":                       # melon (prio 100)
        return 100
    if "WATER" in s.acts and s.tile is not None:
        tl = tile_at(W, s.tile)
        if isinstance(tl, dict) and int(tl.get("consecutive_unwatered", 0)) >= 1:
            return 98
    if s.kind == "animal" and "FEED" in s.acts:
        return 95
    if ROUTE_CARE_RANK and s.kind == "animal" and ("CARE" in s.acts or "HARVEST" in s.acts
                                                   or (ROUTE_COLLECT_RANK and "COLLECT_FERTILIZER" in s.acts)):
        # the feed is done and the unit is already on the tile: the remaining CARE is
        # one product unit ($130-230) for one hour with no walk, the HARVEST a
        # premium unit on hand.  Pruning it (refreshed prio 70, the lowest A) was
        # the main source of fed-but-uncared animal-days (2.6 per game).
        return ROUTE_CARE_RANK
    if s.kind == "place":
        return 90
    if s.kind == "plant" and len(s.acts) > 1 and s.acts[1] == "STRAWBERRY":
        return 88
    return s.prio


def _prune_route(route, budget, start, W):
    """Remove the least valuable stops until the route fits the budget.
    Returns (route, removed_stops)."""
    removed = []
    while route and _route_time(route, start) > budget:
        # pickups and melon harvests (day-10 dump race) are never pruned
        cand = [s for s in route if s.kind not in ("PICKUP", "harvest")]
        if not cand:
            break
        victim = min(cand, key=lambda s: (-s.cls, _a_rank(s, W) if s.cls == 0 else s.prio, -_walk(start, _stop_pos(s))))
        route = [s for s in route if s is not victim]
        if victim.kind != "DROP":
            removed.append(victim)
    return route, removed


def _insert_best(route, stop, start, room, first_free):
    """Cheapest insertion position of stop into route (positions >= first_free).
    Returns (delta_time, pos) or (None, None) if delta > room."""
    best = None
    bpos = None
    n = len(route)
    sp = _stop_pos(stop)
    for pos in range(first_free, n + 1):
        prev = _stop_pos(route[pos - 1]) if pos > 0 else start
        nxt = _stop_pos(route[pos]) if pos < n else None
        d = _walk(prev, sp) + stop.t
        if pos < n:
            d += _walk(sp, nxt) - _walk(prev, nxt)
        if best is None or d < best:
            best, bpos = d, pos
    if best is None or best > room:
        return None, None
    return best, bpos


def _lead_pickups(route):
    i = 0
    while i < len(route) and route[i].kind == "PICKUP":
        i += 1
    return i


def _lead_fixed(route):
    """Leading stops nothing may be inserted before: pickups and melon harvests."""
    i = _lead_pickups(route)
    while i < len(route) and route[i].kind in ("harvest", "DROP"):
        i += 1
    return i


def _cls_cap(cls, prio):
    if cls == 0:
        return 99
    if cls == 1:
        return 4
    return 1 if prio <= 20 else 2


def _pool_insert(routes, budgets, s, W, starts, bump, invs=None):
    """Insert stop s into the cheapest feasible route.  budgets[k] = turns the
    route may still take from starts[k]; invs = current inventories (day) or
    None (planning).  Returns list of bumped stops or None if not placed."""
    best = None
    for k, r in enumerate(routes):
        start = starts[k]
        used = _route_time(r, start)
        room = budgets[k] - used
        cap = _cls_cap(s.cls, s.prio)
        first_free = _lead_fixed(r)
        extra_need = 0
        if s.need is not None and invs is not None:
            iv = invs[k] if k < len(invs) else {}
            has = int(iv.get(s.need, 0)) > 0 or any(x.kind == "PICKUP" and x.item == s.need for x in r)
            if not has and s.need == "FERTILIZER":
                has = any("COLLECT_FERTILIZER" in x.acts for x in r)
            if not has:
                if int(W.shed.get(s.need, 0)) <= 0:
                    continue
                extra_need = 1 + _walk(start, None)      # rough shed detour
        d, pos = _insert_best(r, s, start, room - 1 - extra_need, first_free)
        if d is None:
            if bump and s.cls == 0:
                # force it in, then let the rank-based pruning decide what
                # leaves this route (never s itself, else not placeable here)
                d2, pos2 = _insert_best(r, s, start, 999, first_free)
                if d2 is None:
                    continue
                trial = list(r)
                trial.insert(pos2, s)
                kept, victims = _prune_route(trial, budgets[k] - 1 - extra_need, start, W)
                if s in kept:
                    cand = (d2 + extra_need + 50 + 10 * len(victims), k, pos2, victims)
                    if best is None or cand[0] < best[0]:
                        best = cand
            continue
        if d - s.t > cap:
            continue
        cand = (d + extra_need, k, pos, [])
        if best is None or cand[0] < best[0]:
            best = cand
    if best is None:
        return None
    _, k, pos, victims = best
    r = routes[k]
    for v in victims:
        if v in r:
            r.remove(v)
    pos = min(pos, len(r))
    r.insert(pos, s)
    return victims


def _provisional_plants(st, W, jobs):
    """Empty tiles that will get a PLANT job as soon as the market delivers the
    seeds (the h0 market buys them; the plan should already route them)."""
    have = set(j.key for j in jobs)
    out = []
    day = W.day
    prio_w = 66 if day >= PLANT_T2_DAY else 58
    for (x, y), role in st.roles.items():
        t = (x, y)
        if (t, "plant") in have:
            continue
        tl = W.tiles[y][x]
        if tl is not None:
            continue
        if role == "WHEAT" and day <= WHEAT_LAST_PLANT and day >= 1:
            out.append(Job(t, ["PLANT", "WHEAT"], prio_w, kind="plant"))
        elif role == "STRAW" and 3 <= day <= max(STRAW_LAST_PLANT, STRAW_LATE_LAST):
            out.append(Job(t, ["PLANT", "STRAWBERRY"], PLANT_STRAW_PRIO, kind="plant"))
        elif role == "TOMATO" and day <= ADAPT_TOMATO_LAST:
            out.append(Job(t, ["PLANT", "TOMATO"], PLANT_STRAW_PRIO, kind="plant"))
        elif role in ("STRAW", "MELON", "CARROT", "TOMATO") and day <= WHEAT_LAST_PLANT and day >= 1 and not (
                role == "MELON" and day <= st.melon_plant_day) and not (
                STRAW_TILE_HOLD and role == "STRAW" and day >= 3 and day <= STRAW_LAST_PLANT
                and st.straw_target > count_crop(W, "STRAWBERRY")
                and int(W.seeds.get("STRAWBERRY", 0)) + int(max(0, W.money - 200) // CROPS["STRAWBERRY"]["seed"]) > 0):
            out.append(Job(t, ["PLANT", "WHEAT"], prio_w - 2, kind="plant"))
    return out


def plan_routes(st, W, jobs):
    """Build the day's routes (hour 0): real units + virtual hands."""
    day, hour = W.day, W.hour
    n_real = len(W.units)
    n_virtual = 0
    if hour <= 3:
        n_virtual = max(0, st.hires_wanted - (n_real - 1))
    starts = []
    budgets = []
    owners = []
    for i in range(n_real):
        owners.append(i)
        starts.append(W.units[i] if hour > 0 else None)
        budgets.append(24 - hour - ROUTE_SLACK)
    for _ in range(n_virtual):
        owners.append(None)
        starts.append(None)
        budgets.append(24 - (hour + 1) - ROUTE_SLACK)
    n_slots = len(owners)
    stops = [_stop_from_job(j, W) for j in jobs] + [_stop_from_job(j, W) for j in _provisional_plants(st, W, jobs)]
    melons = [s for s in stops if s.kind == "harvest"]
    ab = [s for s in stops if s.cls <= 1 and s.kind != "harvest"]
    cc = [s for s in stops if s.cls == 2]

    def ang(s):
        return math.atan2(s.tile[1] - CENTER[1], s.tile[0] - CENTER[0])
    ab.sort(key=lambda s: (ang(s), center_dist(s.tile)))
    if len(ab) > 2:
        gaps = []
        for i in range(len(ab)):
            g = (ang(ab[(i + 1) % len(ab)]) - ang(ab[i])) % (2 * math.pi)
            gaps.append((g, i))
        _, gi = max(gaps)
        ab = ab[gi + 1:] + ab[:gi + 1]
    total_est = sum(s.t + 1.2 for s in ab)
    routes = [[] for _ in range(n_slots)]
    leftover = []
    slot = 0
    cur = []
    cur_t = 0
    done_est = 0.0
    for s in ab:
        if slot >= n_slots:
            leftover.append(s)
            continue
        # adaptive target: spread the remaining work over the remaining slots
        mean = (total_est - done_est) / max(1, n_slots - slot)
        target = min(budgets[slot], max(6.0, mean * ROUTE_BAL))
        d, pos = _insert_best(cur, s, starts[slot], 999, 0)
        if cur and cur_t + d > target:
            routes[slot] = cur
            slot += 1
            cur = [s]
            cur_t = _walk(starts[slot] if slot < n_slots else None, s.tile) + s.t
            if slot >= n_slots:
                leftover.append(s)
                cur = []
                cur_t = 0
        else:
            cur.insert(pos, s)
            cur_t += d
        done_est += s.t + 1.2
    if cur:
        if slot < n_slots:
            routes[slot] = cur
        else:
            leftover.extend(cur)
    routes = [_order_stops(r) if starts[k] is None else r for k, r in enumerate(routes)]
    # balance: move edge stops from long routes to short ones when cheap
    for _ in range(3):
        times = [_route_time(r, starts[k]) for k, r in enumerate(routes)]
        mean_t = sum(times) / max(1, n_slots)
        moved = False
        for k in sorted(range(n_slots), key=lambda k: -times[k]):
            if times[k] <= mean_t + 3 or len(routes[k]) <= 1:
                continue
            for s in (routes[k][0], routes[k][-1]):
                rest = [x for x in routes[k] if x is not s]
                saving = times[k] - _route_time(rest, starts[k])
                best = None
                for k2 in range(n_slots):
                    if k2 == k or times[k2] >= mean_t:
                        continue
                    d, pos = _insert_best(routes[k2], s, starts[k2], min(saving + 2, budgets[k2] - times[k2]), 0)
                    if d is not None and (best is None or d < best[0]):
                        best = (d, k2, pos)
                if best is not None:
                    routes[k] = rest
                    routes[best[1]].insert(best[2], s)
                    moved = True
                    break
            if moved:
                break
        if not moved:
            break
    # leftover A/B: cheapest insertion; A may bump lower-ranked stops, which
    # then try the other routes (worklist ordered by class / rank)
    pool_c = list(cc)
    work = sorted(leftover, key=lambda s: (s.cls, -_a_rank(s, W), -s.prio))
    guard = 0
    while work and guard < ROUTE_PLAN_WORK_MAX:
        guard += 1
        s = work.pop(0)
        victims = _pool_insert(routes, budgets, s, W, starts, bump=(s.cls == 0))
        if victims is None:
            pool_c.append(s)
            continue
        for v in victims:
            if v.cls == 0:
                work.append(v)
            else:
                pool_c.append(v)
        work.sort(key=lambda s: (s.cls, -_a_rank(s, W), -s.prio))
    # class C: only cheap insertions (capped: idle units pick up the rest)
    for k_c, s in enumerate(sorted(pool_c, key=lambda s: (s.cls, -s.prio, center_dist(s.tile)))):
        if k_c >= ROUTE_PLAN_C_MAX:
            break
        _pool_insert(routes, budgets, s, W, starts, bump=False)
    # melon harvests (day-10 dump = price race): spread them, one per route,
    # in front of everything else, least loaded route first
    for s in sorted(melons, key=lambda s: -center_dist(s.tile)):
        best = None
        for k in range(n_slots):
            n_mel = sum(1 for x in routes[k] if x.kind == "harvest")
            used = _route_time(routes[k], starts[k])
            cost = 2 * _walk(starts[k], s.tile) + s.t + 1        # out, harvest, back to the shed, drop
            # class-A work already in the route must survive: only B/C may be pruned
            a_time = sum(x.t + 1 for x in routes[k] if x.cls == 0)
            fits = used + cost - sum(x.t + 1 for x in routes[k] if x.cls > 0) <= budgets[k] or a_time == 0
            cand = (n_mel, 0 if fits else 1, used + cost, k)
            if best is None or cand < best:
                best = cand
        routes[best[3]].insert(0, s)
    # pickups and drops; then enforce the budget (least valuable stops out,
    # class-A leftovers try the other routes)
    shed_left = {"FERTILIZER": int(W.shed.get("FERTILIZER", 0))}
    for k in range(n_slots):
        r = _plan_drops(routes[k], W, budgets[k])
        routes[k] = _plan_pickups(r, W, shed_left)
    spill = []
    for k in range(n_slots):
        routes[k], rem = _prune_route(routes[k], budgets[k], starts[k], W)
        spill.extend(rem)
    work = sorted([x for x in spill if x.cls == 0], key=lambda s: (-_a_rank(s, W), -s.prio))
    guard = 0
    while work and guard < 200:
        guard += 1
        s = work.pop(0)
        victims = _pool_insert(routes, budgets, s, W, starts, bump=True)
        if victims:
            work.extend(v for v in victims if v.cls == 0)
            work.sort(key=lambda s: (-_a_rank(s, W), -s.prio))
    st.routes = {}
    st.vroutes = []
    for k, ui in enumerate(owners):
        if ui is not None:
            st.routes[ui] = routes[k]
        else:
            st.vroutes.append(routes[k])
    st.route_day = day
    st.route_skip = {}
    st.route_hands = -1


def _doable(a, job, iv, seed_left):
    if a == "FERTILIZE":
        return int(iv.get("FERTILIZER", 0)) > 0
    if a == "FEED":
        return int(iv.get("WHEAT", 0)) > 0
    if a == "PLACE":
        return int(iv.get(job.actions[1], 0)) > 0
    if a == "PLANT":
        return seed_left.get(job.actions[1], 0) > 0
    return True


def _first_action(job, iv, seed_left):
    a0 = job.actions[0]
    if a0 in ("PLACE", "PLANT"):
        return a0 if _doable(a0, job, iv, seed_left) else None
    for a in job.actions:
        if _doable(a, job, iv, seed_left):
            return a
    return None


def route_step(st, W, jobs):
    """Route executor: returns unit actions (index aligned with W.units)."""
    n_units = len(W.units)
    day, hour = W.day, W.hour
    actions = [["PASS"] for _ in range(n_units)]
    hours_left = 24 - hour
    fresh = {}
    for j in jobs:
        fresh[j.key] = j
    invs = [dict(iv) for iv in W.invs]
    shed = dict(W.shed)
    shed_room = SHED_CAP - shed_total(W)
    seed_left = dict(W.seeds)
    routes = st.routes
    # new units take virtual routes; after the hire window virtual routes dissolve
    for i in range(n_units):
        if i in routes:
            continue
        if st.vroutes:
            pos = W.units[i]

            def first_pos(r):
                for s in r:
                    p = _stop_pos(s)
                    if p is not None:
                        return p
                return None
            bi = min(range(len(st.vroutes)), key=lambda k: _walk(pos, first_pos(st.vroutes[k])))
            routes[i] = st.vroutes.pop(bi)
        else:
            routes[i] = []
    dissolved = []
    n_hands = n_units - 1
    no_more = (n_hands >= st.hires_wanted) or (hour >= 2 and n_hands <= getattr(st, "route_hands", -1))
    st.route_hands = n_hands
    if st.vroutes and (hour > 3 or no_more):
        for r in st.vroutes:
            dissolved.extend(s for s in r if s.kind not in ("PICKUP", "DROP"))
        st.vroutes = []

    # validate routes: drop stops whose job is gone (provisional plants stay while the tile is empty)
    def alive(s):
        if s.kind in ("PICKUP", "DROP"):
            return True
        if s.key in fresh:
            return True
        if s.kind == "plant" and tile_at(W, s.tile) is None and day <= WHEAT_LAST_PLANT:
            return True
        return False
    for i in range(n_units):
        r = [s for s in routes[i] if alive(s)]
        for s in r:
            j = fresh.get(s.key)
            if j is not None:
                # refresh what is left to do on the tile (e.g. FEED done -> no wheat needed)
                s.acts = j.actions
                s.need = j.need
                s.prio = j.prio
                s.t = _job_time(j, W)
        if any(s.kind == "PICKUP" for s in r):
            r = [s for s in r if not (s.kind == "PICKUP" and s.item in ("WHEAT", "FERTILIZER")
                                      and not any(x.need == s.item for x in r))]
        # still at the shed with feed stops but no pickup planned (feed jobs
        # appeared after the plan): add the bulk pickup now
        if (r and nearest_shed(W.units[i])[1] == 0 and int(invs[i].get("WHEAT", 0)) <= 0
                and not any(s.kind == "PICKUP" and s.item == "WHEAT" for s in r)):
            n_feed = sum(1 for s in r if s.need == "WHEAT")
            if n_feed > 0:
                r.insert(0, Stop(None, "PICKUP", key=("PICKUP", "WHEAT"), item="WHEAT", k=n_feed, cls=0, t=1))
        routes[i] = r
    route_keys = set()
    for i in range(n_units):
        for s in routes[i]:
            if s.key is not None:
                route_keys.add(s.key)
    for r in st.vroutes:
        for s in r:
            if s.key is not None:
                route_keys.add(s.key)
    # overloaded route: shed the least valuable stops (they go to the pool)
    for i in range(n_units):
        r = routes[i]
        if not r:
            continue
        keep, rem = _prune_route(r, hours_left, W.units[i], W)
        if len(keep) != len(r):
            for s in r:
                if s not in keep and s.key is not None:
                    route_keys.discard(s.key)
            dissolved.extend(rem)
            routes[i] = keep
    # pool: fresh jobs not in any route
    pool = []
    seen = set()
    for j in jobs:
        if j.key in route_keys or j.key in seen:
            continue
        pool.append(_stop_from_job(j, W))
        seen.add(j.key)
    unit_pos = list(W.units)
    hl = [hours_left] * n_units
    # on-tile continuation: a unit standing on a tile that has an unrouted job
    # does it now (no walking) when its route has the slack
    for i in range(n_units):
        pos = W.units[i]
        cand = [s for s in pool if s.tile == pos]
        if not cand:
            continue
        r = routes[i]
        used = _route_time(r, pos)
        iv = invs[i]
        for s in sorted(cand, key=lambda s: (s.cls, -s.prio)):
            if s.key in route_keys:
                continue
            if s.need is not None and int(iv.get(s.need, 0)) <= 0 and not (
                    PARTIAL_NEED and s.need == "FERTILIZER" and len(s.acts) > 1):
                continue
            if used + s.t <= hours_left or s.cls == 0:
                r.insert(_lead_pickups(r), s)
                route_keys.add(s.key)
                used += s.t
                pool.remove(s)
    # pool insertion: A/B by cheapest insertion; C is taken by idle units only
    pool.sort(key=lambda s: (s.cls, -s.prio))
    still = []
    rlist = [routes[i] for i in range(n_units)]
    work = [s for s in pool if s.cls <= 1]
    still = [s for s in pool if s.cls == 2]
    guard = 0
    while work and guard < 300:
        guard += 1
        s = work.pop(0)
        victims = _pool_insert(rlist, hl, s, W, unit_pos, bump=(s.cls == 0), invs=invs)
        if victims is None:
            still.append(s)
            continue
        route_keys.add(s.key)
        if s.need in ("WHEAT", "FERTILIZER"):
            for k in range(n_units):
                if s in rlist[k]:
                    r = rlist[k]
                    lead = [x for x in r[:_lead_pickups(r)] if x.item == s.need]
                    if lead:
                        lead[0].k += 1
                    elif nearest_shed(unit_pos[k])[1] == 0 and int(invs[k].get(s.need, 0)) <= 0:
                        r.insert(0, Stop(None, "PICKUP", key=("PICKUP", s.need), item=s.need, k=1, cls=0, t=1))
                    break
        for v in victims:
            if v.key is not None:
                route_keys.discard(v.key)
                if v.cls == 0:
                    work.append(v)
                else:
                    still.append(v)
        work.sort(key=lambda s: (s.cls, -_a_rank(s, W), -s.prio))
    pool = still
    # ---- execute ----
    fetch_reserved = {}
    for i in range(n_units):
        pos = W.units[i]
        iv = invs[i]
        r = routes[i]
        sd, dsh = nearest_shed(pos)
        prem_val = _premium_value(iv, W)
        cnt = sum(int(v) for k, v in iv.items() if k in MARKET_PARAMS)
        # banking: standing on a shed tile with goods, or the v12 value/distance
        # rule when the route has the slack for the detour (melons: always)
        bank = False
        if cnt > 0 and shed_room > 0:
            if dsh == 0 and prem_val >= ROUTE_PASS_VAL:
                bank = True
            elif _v12_must_drop(iv, W, dsh, hour, shed_room):
                slack = hours_left - _route_time(r, pos)
                if int(iv.get("MELON", 0)) >= 6 or dsh <= 2 or slack >= 2 * dsh - 2 or (r and r[0].kind == "DROP"):
                    bank = True
            if (not bank and ROUTE_SUNSET_H and hour >= ROUTE_SUNSET_H and day < LAST_DAY
                    and cnt >= ROUTE_SUNSET_MIN and dsh <= hours_left - 1):
                # sunset: goods still carried at night are auto-dropped and sold at
                # h0 together with the opponent's batch; walking home now sells them
                # this evening, ahead of it (the v12 SUNSET rule, missing in ROUTE)
                slack = hours_left - _route_time(r, pos)
                if slack >= dsh or (r and r[0].kind == "DROP") or not r:
                    bank = True
        if (bank and r and r[0].kind not in ("PICKUP", "DROP") and pos == r[0].tile and dsh > 0
                and int(iv.get("MELON", 0)) < 6):
            job = fresh.get(r[0].key)
            if job is not None and len(job.actions) <= 1 and _first_action(job, iv, seed_left) is not None:
                bank = False           # one action left here: finish it, bank next hour
        if bank:
            if dsh == 0:
                a = _bank_action(iv, r, W, shed_room)
                if a is not None:
                    actions[i] = a
                    shed_room -= cnt if a[0] == "DROP" else a[2]
                    continue
            else:
                actions[i] = [step_toward(pos, sd)]
                continue
        guard = 0
        acted = False
        while r and guard < 40 and not acted:
            guard += 1
            s = r[0]
            if s.kind == "PICKUP":
                if dsh == 0:
                    avail = int(shed.get(s.item, 0)) - fetch_reserved.get(s.item, 0)
                    if avail >= s.k or (avail >= 1 and (hour > 2 or s.item != "WHEAT")):
                        k = max(1, min(s.k, avail))
                        fetch_reserved[s.item] = fetch_reserved.get(s.item, 0) + k
                        actions[i] = ["PICKUP", s.item, k]
                        r.pop(0)
                        acted = True
                    elif hour <= 2 and s.item == "WHEAT":
                        actions[i] = ["PASS"]        # the market phase will stock it
                        acted = True
                    else:
                        r.pop(0)
                else:
                    actions[i] = [step_toward(pos, sd)]
                    acted = True
                continue
            if s.kind == "DROP":
                if dsh == 0:
                    r.pop(0)
                    if cnt > 0 and shed_room > 0:
                        a = _bank_action(iv, r, W, shed_room)
                        if a is not None:
                            actions[i] = a
                            shed_room -= cnt if a[0] == "DROP" else a[2]
                            acted = True
                    continue
                actions[i] = [step_toward(pos, sd)]
                acted = True
                continue
            job = fresh.get(s.key)
            if job is None:
                if s.kind == "plant" and pos == s.tile and hour <= 3 and tile_at(W, s.tile) is None:
                    actions[i] = ["PASS"]            # seeds arrive with the next market phase
                    acted = True
                    continue
                r.pop(0)
                continue
            # need an item we do not carry?
            if s.need is not None and int(iv.get(s.need, 0)) <= 0:
                partial = PARTIAL_NEED and s.need == "FERTILIZER" and _partial_ok(job, iv)
                if not partial:
                    avail = int(shed.get(s.need, 0)) - fetch_reserved.get(s.need, 0)
                    if avail >= 1:
                        if dsh == 0:
                            k = 1
                            if s.need in ("WHEAT", "FERTILIZER"):
                                k = max(1, min(avail, sum(1 for x in r if x.need == s.need)))
                            fetch_reserved[s.need] = fetch_reserved.get(s.need, 0) + k
                            actions[i] = ["PICKUP", s.need, k]
                        else:
                            actions[i] = [step_toward(pos, sd)]
                        acted = True
                        continue
                    if (ROUTE_WAIT_WHEAT and s.need == "WHEAT" and hour <= 20 and day < LAST_DAY
                            and W.money >= 2 * (W.prices.get("WHEAT", 25) + 3) + 5
                            and dsh + 2 <= hours_left):
                        # the shed is dry but the market phase of this step buys feed wheat
                        # (market_orders: need_today > carried + shed): walk to the shed and
                        # wait one hour instead of abandoning the animal for the day
                        wait_key = ("WAITW", i)
                        waited = st.route_skip.setdefault(i, set())
                        if dsh > 0:
                            actions[i] = [step_toward(pos, sd)]
                            acted = True
                            continue
                        if wait_key not in waited:
                            waited.add(wait_key)
                            actions[i] = ["PASS"]
                            acted = True
                            continue
                        waited.discard(wait_key)
                    r.pop(0)
                    st.route_skip.setdefault(i, set()).add(s.key)
                    continue
            if pos != s.tile:
                if prem_val >= ROUTE_PASS_VAL and dsh > 0:
                    # pass a shed tile on the way (free) to bank premium goods
                    for sh in SHED_TILES:
                        if dist(pos, sh) + dist(sh, s.tile) == dist(pos, s.tile):
                            actions[i] = [step_toward(pos, sh)]
                            acted = True
                            break
                    if acted:
                        continue
                actions[i] = [step_toward(pos, s.tile)]
                acted = True
                continue
            a0 = _first_action(job, iv, seed_left)
            if a0 is None:
                r.pop(0)
                st.route_skip.setdefault(i, set()).add(s.key)
                continue
            if a0 in ("PLACE", "PLANT"):
                actions[i] = [a0, job.actions[1]]
                if a0 == "PLANT":
                    seed_left[job.actions[1]] = seed_left.get(job.actions[1], 0) - 1
            else:
                actions[i] = [a0]
            acted = True
        if acted:
            continue
        # ---- idle unit: bank premium goods (sold today, ahead of the h0
        # lockstep dump), else nearest pool job, else drift ----
        if prem_val >= ROUTE_PASS_VAL and shed_room > 0 and dsh <= hours_left:
            near_a = any(s.cls == 0 and s.key not in route_keys and dist(pos, s.tile) <= 1
                         and (s.need is None or int(iv.get(s.need, 0)) > 0) for s in pool)
            if not near_a:
                if dsh == 0:
                    a = _bank_action(iv, r, W, shed_room)
                    if a is not None:
                        actions[i] = a
                        shed_room -= cnt if a[0] == "DROP" else a[2]
                        continue
                else:
                    actions[i] = [step_toward(pos, sd)]
                    continue
        best = None
        for s in pool:
            if s.key in route_keys:
                continue
            d = dist(pos, s.tile)
            if s.cls == 2 and d > ROUTE_IDLE_D:
                continue
            if d + s.t > hours_left:
                continue
            if s.need is not None and int(iv.get(s.need, 0)) <= 0:
                if int(shed.get(s.need, 0)) - fetch_reserved.get(s.need, 0) <= 0:
                    continue
                if s.key in st.route_skip.get(i, ()):
                    continue
                d += dsh + 1
            sc = (s.cls, d, -s.prio)
            if best is None or sc < best[0]:
                best = (sc, s)
        if best is not None:
            s = best[1]
            r.append(s)
            route_keys.add(s.key)
            pool.remove(s)
            job = fresh[s.key]
            if pos == s.tile:
                a0 = _first_action(job, iv, seed_left)
                if a0 is not None:
                    actions[i] = [a0, job.actions[1]] if a0 in ("PLACE", "PLANT") else [a0]
                    if a0 == "PLANT":
                        seed_left[job.actions[1]] = seed_left.get(job.actions[1], 0) - 1
                    continue
            if s.need is not None and int(iv.get(s.need, 0)) <= 0:
                if dsh == 0:
                    fetch_reserved[s.need] = fetch_reserved.get(s.need, 0) + 1
                    actions[i] = ["PICKUP", s.need, 1]
                else:
                    actions[i] = [step_toward(pos, sd)]
            else:
                actions[i] = [step_toward(pos, s.tile)]
            continue
        if cnt > 0:
            actions[i] = ["DROP"] if dsh == 0 else [step_toward(pos, sd)]
        elif dsh > 1:
            actions[i] = [step_toward(pos, sd)]
        else:
            actions[i] = ["PASS"]
    st.routes = routes
    return actions



# ----------------------------------------------------------------------------
# market
# ----------------------------------------------------------------------------
def daily_drain(W, item):
    """Units per day the town removes from the market for this product."""
    d = 1 if item != "FERTILIZER" else 0
    d += shop_count(W, item) * (TPD // SHOP_SELL_INT)
    return d


def premium_floor(item, W):
    """Minimum sale price. Premium goods glut fast and the seller who moves
    first gets the price, so we sell nearly everything immediately and only
    hold when the market is deeply glutted AND the town drain will lift it."""
    base = MARKET_PARAMS[item]["base"]
    if W.step >= LAST_STEP - 1:
        return 1
    day = W.day
    if item == "MELON":
        return 1
    drain = daily_drain(W, item)
    if item == "WOOL":
        f = 0.55 if drain >= 7 else 0.20
    elif item == "MILK":
        f = FLOOR_MILK_HI if drain >= 7 else FLOOR_MILK_LO
    else:  # strawberry
        f = FLOOR_STRAW_HI if drain >= 7 else FLOOR_STRAW_LO
    if EVENING_SELL_H and W.hour >= EVENING_SELL_H and day < 25:
        # sell this evening into the day's drain recovery rather than holding for
        # the h0 batch (which lands together with the night auto-drop and crashes
        # the linear-glut price into my own tail units)
        f = f * EVENING_SELL_F
    if day >= 25:
        # linear decay to 0 at last step
        frac = (LAST_STEP - W.step) / float(LAST_STEP - 25 * TPD)
        f = f * max(0.0, min(1.0, frac))
    return max(1, int(base * f))


def _shed_after_field(W, actions):
    """Shed contents after this step's field phase (DROP/PICKUP on shed tiles
    happen before the market phase), so goods dropped now can be sold now."""
    shed = dict(W.shed)
    room = SHED_CAP - sum(v for v in shed.values() if v)
    for i, pos in enumerate(W.units):
        a = actions[i] if i < len(actions) else None
        if not a or pos not in SHED_TILES:
            continue
        if a[0] == "DROP":
            for it, n in W.invs[i].items():
                n = int(n)
                if n <= 0:
                    continue
                k = min(n, max(0, room))
                if k > 0:
                    shed[it] = int(shed.get(it, 0)) + k
                    room -= k
        elif a[0] == "PICKUP" and len(a) >= 2:
            k = int(a[2]) if len(a) >= 3 else 1
            k = min(k, int(shed.get(a[1], 0)))
            shed[a[1]] = int(shed.get(a[1], 0)) - max(0, k)
            room += max(0, k)
        elif a[0] == "PLACE" and len(a) >= 2 and a[1] not in ANIMALS:
            # selective shed drop (v13 route executor): PLACE item n on a shed tile
            k = int(a[2]) if len(a) >= 3 else 1
            k = min(k, int(W.invs[i].get(a[1], 0)), max(0, room))
            if k > 0:
                shed[a[1]] = int(shed.get(a[1], 0)) + k
                room -= k
    return shed


def _animal_orders(st, W, orders, cash, cash_reserve, n_cows, n_sheep, day, hour, endgame):
    """The animal purchase block of market_orders (callable early, see ANIMALS_FIRST)."""
    # ---------------- animals ----------------
    if not endgame and day <= SHEEP_LAST_BUY and hour <= 6 and len(orders) < 10:
        # how many pastures exist / can host
        empty_pastures = 0
        empty_animal_tiles = 0
        for (x, y), role in st.roles.items():
            tl = W.tiles[y][x]
            if is_empty_struct(tl) and tl.get("kind") == "PASTURE":
                empty_pastures += 1
            if role == "ANIMAL" and tl is None:
                empty_animal_tiles += 1
        capacity = empty_pastures + empty_animal_tiles
        unplaced = (n_cows - count_animals(W, "COW")) + (n_sheep - count_animals(W, "SHEEP"))
        capacity -= unplaced
        buy_c = max(0, st.cow_target - n_cows)
        buy_s = max(0, st.sheep_target - n_sheep)
        # buy limited by cash & capacity, prefer cows early -- unless a yarn store is
        # already open: then the sheep are the scarcer, higher-value animal and the
        # top agents (islet: sheep 6@d8, 10@d10) buy them before the cow ramp
        order_list = []
        buy_order = (("COW", buy_c), ("SHEEP", buy_s))
        if (ANIMAL_SHEEP_FIRST or SHEEP_BEFORE_SEEDS) and shop_count(W, "WOOL") // 2 >= 1 and buy_s > 0:
            buy_order = (("SHEEP", buy_s), ("COW", buy_c))
        for kind, cnt in buy_order:
            k = 0
            while k < cnt and capacity > 0:
                c = ANIMALS[kind]["cost"]
                if cash - c < cash_reserve * ANIMAL_RESERVE_SCALE:
                    break
                cash -= c
                capacity -= 1
                k += 1
            if k > 0:
                order_list.append(["BUY_ANIMAL", kind, k])
        if ADAPT and ADAPT_GEESE:
            n_geese = owned_animals(W, "GOOSE")
            empty_coops = sum(1 for row in W.tiles for t in row if is_empty_struct(t) and t.get("kind") == "COOP")
            unplaced_g = n_geese - count_animals(W, "GOOSE")
            cap_g = empty_coops - unplaced_g
            k = 0
            while k < max(0, getattr(st, "goose_target", 0) - n_geese) and cap_g > 0:
                c = ANIMALS["GOOSE"]["cost"]
                if cash - c < cash_reserve * ANIMAL_RESERVE_SCALE:
                    break
                cash -= c
                cap_g -= 1
                k += 1
            if k > 0:
                order_list.append(["BUY_ANIMAL", "GOOSE", k])
        orders.extend(order_list)

    return cash


def market_orders(st, W, actions):
    orders = []
    day, hour = W.day, W.hour
    endgame = day >= LAST_DAY
    money = W.money
    shed = _shed_after_field(W, actions) if SELL_SAME_STEP else W.shed
    n_animals = count_animals(W)
    n_cows = owned_animals(W, "COW")
    n_sheep = owned_animals(W, "SHEEP")
    # cash we must keep for tomorrow's hires and feed
    cash_reserve = hire_total(min(12, st.hires_wanted)) + n_animals * 45 + 60
    if day == 0:
        cash_reserve = 0

    # ---------------- sells ----------------
    # planned fertilizer needs (only imminent ones)
    fert_need = 0
    for (x, y), role in st.roles.items():
        tl = W.tiles[y][x]
        if is_plant(tl):
            age = day - int(tl.get("planted_day", day))
            crop = tl.get("crop")
            fu = int(tl.get("fertilized_until_day", -1))
            if crop == "STRAWBERRY" and (age in (8, 9, 11, 12, 13, 15)) and fu < day:
                fert_need += 1
    fert_need = min(fert_need, 30)
    if day >= 27:
        fert_need = 0
    if money < 40 and day < 8:
        fert_need = 0            # cash emergency: feed comes first
    fert = int(shed.get("FERTILIZER", 0))
    if FERT_EARLY_SELL and day < FERT_EARLY_LAST and W.prices.get("FERTILIZER", 100) >= FERT_EARLY_MIN:
        # fertilizer has no town drain: every unit lowers the price for good, so the
        # early units are the valuable ones (islet sells 8-19/day from day 2 at $60-80
        # and BUYS back the ~50 it needs for strawberries at ~$37; I hoarded mine for
        # the fertilizations and sold 269 units at h1 of days 19-29 for $34).  Keep
        # only today's fertilizations; buy back the rest the morning it is needed.
        fert_need = 0
        for (x, y), role in st.roles.items():
            tl = W.tiles[y][x]
            if is_plant(tl) and tl.get("crop") == "STRAWBERRY":
                age = day - int(tl.get("planted_day", day))
                if age in (9, 13) and int(tl.get("fertilized_until_day", -1)) < day:
                    fert_need += 1
        fert_need = min(fert_need, 30)
    sell_f = fert - fert_need
    if FERT_SELL_MAX > 0 and sell_f > 0 and not endgame and shed_total(W) <= 80 and day < 27:
        # pace fertilizer (no town drain, linear -$0.2/unit): a morning bulk sale
        # crashes the price into my own units (my avg $34 vs islet's $42 on the
        # same volume); a few units per step keep the price near the top
        sell_f = min(sell_f, FERT_SELL_MAX)
    if sell_f > 0 and (W.prices.get("FERTILIZER", 100) >= FERT_SELL_MIN or endgame or shed_total(W) > 80):
        orders.append(["SELL", "FERTILIZER", sell_f])

    wheat = int(shed.get("WHEAT", 0))
    days_left = LAST_DAY - day
    reserve = n_animals * min(FEED_RESERVE_DAYS, max(0, 28 - day + 1)) if day < 28 else 0
    if day < 8 and EARLY_RESERVE_DAYS < 3:
        # early cash: the first wheat harvests are worth more as cows than as a
        # 3-day feed stock (feed is re-bought at ~+$3)
        reserve = n_animals * EARLY_RESERVE_DAYS
    if day >= 27:
        reserve = n_animals * max(0, 28 - day + 1)
    if endgame:
        reserve = 0
    sell_w = wheat - reserve
    if sell_w > 0 and (W.prices.get("WHEAT", 25) >= 22 or endgame or shed_total(W) > 75):
        orders.append(["SELL", "WHEAT", sell_w])

    melon = int(shed.get("MELON", 0))
    if melon > 0:
        orders.append(["SELL", "MELON", melon])

    total = shed_total(W)
    carried_total = sum(sum(int(v) for k, v in iv.items() if k in MARKET_PARAMS) for iv in W.invs)
    # everything carried lands in the shed at the end of the day; whatever does
    # not fit is destroyed -> from the afternoon on, sell down to make room
    projected = total + carried_total
    held_wool = int(shed.get("WOOL", 0)) if (WOOL_HOLD and W.prices.get("WOOL", 200) < WOOL_HOLD_PRICE and day <= WOOL_HOLD_LAST) else 0
    if hour >= 16 and OVERFLOW_SELL:
        overflow = max(0, projected - 88)
    else:
        overflow = max(0, total - min(80 + WOOL_HOLD_SLACK * held_wool, 90))
    # already-planned wheat/fertilizer sales reduce the overflow
    for o in orders:
        overflow -= int(o[2])
    overflow = max(0, overflow)
    if OVERFLOW_WHEAT_FIRST and overflow > 0 and not endgame:
        # make room with the wheat / fertilizer reserve first (deep, flat
        # markets; re-bought tomorrow at ~+$3) instead of dumping premium goods
        # into a glut at $1-20
        w_left = int(shed.get("WHEAT", 0)) - sum(int(o[2]) for o in orders if o[1] == "WHEAT")
        w_keep = n_animals if day < 28 else 0
        k = min(max(0, w_left - w_keep), overflow)
        if k > 0:
            orders.append(["SELL", "WHEAT", k])
            overflow -= k
        f_left = int(shed.get("FERTILIZER", 0)) - sum(int(o[2]) for o in orders if o[1] == "FERTILIZER")
        k = min(max(0, f_left - fert_need), overflow)
        if k > 0:
            orders.append(["SELL", "FERTILIZER", k])
            overflow -= k
    # cheapest-first order for forced sales
    prem = ["MILK", "WOOL", "STRAWBERRY", "EGG", "CARROT", "TOMATO"]
    prem.sort(key=lambda it: W.prices.get(it, MARKET_PARAMS[it]["base"]))
    # wool: a glutted wool market (no yarn store yet) is worth ~$1 now but ~$150
    # a few days after a yarn store unlocks (2/3 of seasons see one); hold it
    # and dump only as the last overflow resort (after wheat/fertilizer reserves)
    hold_wool = (WOOL_HOLD and not endgame and day <= WOOL_HOLD_LAST
                 and W.prices.get("WOOL", 200) < WOOL_HOLD_PRICE)
    for item in prem:
        n = int(shed.get(item, 0))
        if n <= 0:
            continue
        inv = W.inv.get(item, MARKET_I0)
        if item in ("EGG", "CARROT", "TOMATO"):
            floor_p = 1 if endgame else int(MARKET_PARAMS[item]["base"] * 0.7)
        else:
            floor_p = premium_floor(item, W)
        k = units_above_price(item, inv, floor_p, n)
        if item == "WOOL" and hold_wool:
            continue
        if SELL_HOUR_ONLY and item in ("MILK", "WOOL", "STRAWBERRY") and not endgame and day < 27:
            # sell premium goods first thing in the morning (before the typical
            # opponent's hour-1 dump, after the overnight town drain), not during
            # the day into a depressed market
            if hour > SELL_HOUR_LAST:
                k = 0
        if overflow > 0 and k < n:
            extra = min(n - k, overflow)
            k += extra
            overflow -= extra
        if endgame:
            k = n
        if k > 0:
            orders.append(["SELL", item, k])
    if overflow > 0:
        # still too much: dump wheat reserve / fertilizer reserve too
        w_left = int(shed.get("WHEAT", 0)) - sum(int(o[2]) for o in orders if o[1] == "WHEAT")
        if w_left > 0:
            k = min(w_left, overflow)
            orders.append(["SELL", "WHEAT", k])
            overflow -= k
        f_left = int(shed.get("FERTILIZER", 0)) - sum(int(o[2]) for o in orders if o[1] == "FERTILIZER")
        if overflow > 0 and f_left > 0:
            k = min(f_left, overflow)
            orders.append(["SELL", "FERTILIZER", k])
            overflow -= k
        if overflow > 0 and hold_wool and int(shed.get("WOOL", 0)) > 0:
            orders.append(["SELL", "WOOL", min(int(shed.get("WOOL", 0)), overflow)])
    if PREEMPT and not endgame:
        orders = _preempt_sells(st, W, orders, shed)
    # estimated proceeds (conservative) to fund purchases this turn
    proceeds = 0.0
    for o in orders:
        if o[0] == "SELL":
            inv = W.inv.get(o[1], MARKET_I0)
            tot = 0
            for _ in range(int(o[2])):
                pr = market_price(o[1], inv)
                tot += pr
                if pr > 1:
                    inv += 1
            proceeds += tot
    cash = money + 0.9 * proceeds

    # animals before hires / land / seeds in the early days (the 09-07 leaders
    # hold 6 cows by day 6 and 11 by day 8; my morning cash went to hires and
    # seeds first and the herd stalled at 2 until day 7 with empty pastures)
    if ANIMALS_FIRST and day <= ANIMALS_FIRST_LAST and (
            (shop_count(W, "MILK") >= COWS_BEFORE_SEEDS_MIN and st.cow_target > n_cows)
            or (shop_count(W, "WOOL") // 2 >= 1 and st.sheep_target > n_sheep)):
        cash = _animal_orders(st, W, orders, cash, cash_reserve, n_cows, n_sheep, day, hour, endgame)
        st.animals_bought_early = True

    # ---------------- hires ----------------
    if not endgame or hour <= 2:
        want = st.hires_wanted
        if endgame:
            want = min(want, 8)
        have = W.hires_today
        n_hire = max(0, want - have)
        # only hire in the first hours of the day
        if hour > 3:
            n_hire = 0
        cost = 0
        k = 0
        while k < n_hire and len(orders) < 10:
            c = fib_cost(have + k)
            if cash - cost - c < 0:
                break
            cost += c
            k += 1
        for _ in range(k):
            orders.append(["HIRE"])
        cash -= cost

    # ---------------- land ----------------
    n_unl = len(W.unlocked)
    if n_unl < 4 and not endgame and day <= 16 and len(orders) < 10:
        price = LAND_PRICES[n_unl - 1]
        ok = False
        cow_deficit = max(0, st.cow_target - n_cows)
        if n_unl == 1 and day >= LAND1_DAY and cash - price >= cash_reserve:
            ok = True
        if n_unl == 2 and day >= 8 and cash - price >= cash_reserve + 400 * cow_deficit + 300:
            ok = True
        # 4th quadrant ($4000): only when the herd needs the room (yarn shift ->
        # 10-12 sheep) and cash is comfortable; the top adaptive agents buy it
        if n_unl == 3 and LAND3_YARN and getattr(st, "yarn_shift", False) and day >= LAND3_DAY and cash - price >= cash_reserve + 1500:
            ok = True
        if ok:
            orders.append(["BUY_LAND"])
            cash -= price

    # ---------------- seeds ----------------
    if not endgame and len(orders) < 10:
        want = {}
        for (x, y), role in st.roles.items():
            tl = W.tiles[y][x]
            if tl is None or is_weed(tl):
                if role == "MELON" and day == st.melon_plant_day:
                    want["MELON"] = want.get("MELON", 0) + 1
                elif role == "STRAW" and day >= 3 and day <= max(STRAW_LAST_PLANT, STRAW_LATE_LAST):
                    want["STRAWBERRY"] = want.get("STRAWBERRY", 0) + 1
                elif role == "CARROT" and day <= CARROT_LAST_PLANT:
                    want["CARROT"] = want.get("CARROT", 0) + 1
                elif role == "TOMATO" and day <= ADAPT_TOMATO_LAST:
                    want["TOMATO"] = want.get("TOMATO", 0) + 1
                elif role in ("WHEAT", "STRAW", "MELON", "CARROT", "TOMATO") and day >= 1 and day <= WHEAT_LAST_PLANT:
                    want["WHEAT"] = want.get("WHEAT", 0) + 1
                elif role == "ANIMAL" and (day <= 1 or day >= 18) and day <= WHEAT_LAST_PLANT and not want_more_structures(st, W):
                    want["WHEAT"] = want.get("WHEAT", 0) + 1
        # only buy for what can be planted today (labor bound) + small buffer
        max_plant_today = max(2, len(W.units) * 2)
        if st.melon_wave2 and day == st.melon_plant_day:
            have = count_crop(W, "MELON")
            planted_w2 = sum(1 for row in W.tiles for t in row if is_plant(t) and t.get("crop") == "MELON"
                             and int(t.get("planted_day", -1)) == day)
            want["MELON"] = max(want.get("MELON", 0), st.melon_wave2_n - planted_w2)
        # yarn draw, cash-starved days: the sheep come before the second strawberry
        # wave (islet buys 2 sheep/day from the day the yarn store opens and plants
        # its second wave after the melon money; I bought seeds + cows first and got
        # my sheep on day 11 -> wool -12k, fertilizer -4k in every yarn game)
        sheep_reserve = 0
        # (all hours: animals are bought at h<=6, so the cash must be held overnight --
        # evening seed purchases used to spend the morning's cow money)
        if (SHEEP_BEFORE_SEEDS and day <= SHEEP_BEFORE_SEEDS_LAST
                and shop_count(W, "WOOL") // 2 >= 1 and st.sheep_target > n_sheep):
            sheep_reserve = min(st.sheep_target - n_sheep, SHEEP_BEFORE_SEEDS_PER_DAY) * ANIMALS["SHEEP"]["cost"]
        if (COWS_BEFORE_SEEDS and day <= COWS_BEFORE_SEEDS_LAST
                and shop_count(W, "MILK") >= COWS_BEFORE_SEEDS_MIN and st.cow_target > n_cows):
            # milk draw: the 09-07 leaders hold 6 cows by day 6 and 11 by day 8 (milk
            # 414u vs my 288u in a 5-milk-shop draw); my early cash went to strawberry
            # seeds first and the herd stalled at 7 by day 8
            sheep_reserve += min(st.cow_target - n_cows, COWS_BEFORE_SEEDS_PER_DAY) * ANIMALS["COW"]["cost"]
        for crop in ("MELON", "STRAWBERRY", "TOMATO", "CARROT", "WHEAT"):
            n = want.get(crop, 0) - int(W.seeds.get(crop, 0))
            if crop == "STRAWBERRY":
                n = min(n, 40)
            elif crop == "WHEAT":
                # keep a buffer so a tile harvested this hour can be replanted at
                # once by the unit standing on it (no idle tile-days)
                n = min(n + SEED_BUFFER, max_plant_today + SEED_BUFFER)
            if n <= 0:
                continue
            c = CROPS[crop]["seed"]
            reserve = cash_reserve if crop not in ("WHEAT", "CARROT") else min(cash_reserve, 120)
            if crop in ("STRAWBERRY", "MELON", "TOMATO"):
                reserve += sheep_reserve
            k = int(max(0, (cash - reserve) // c))
            k = min(k, n)
            if k > 0:
                orders.append(["BUY_SEED", crop, k])
                cash -= k * c

    if not getattr(st, "animals_bought_early", False):
        cash = _animal_orders(st, W, orders, cash, cash_reserve, n_cows, n_sheep, day, hour, endgame)
    st.animals_bought_early = False

    # ---------------- feed wheat ----------------
    if not endgame and len(orders) < 10:
        wheat_now = int(shed.get("WHEAT", 0))
        # wheat carried by units will feed today; tomorrow needs n_animals
        need_today = 0
        for (x, y), role in st.roles.items():
            tl = W.tiles[y][x]
            if has_animal(tl) and not tl.get("fed_today", False):
                need_today += 1
        carried = sum(int(iv.get("WHEAT", 0)) for iv in W.invs)
        if FEED_WHEAT_ALL:
            # animals still in the shed (bought earlier today or being bought right now)
            # are placed and fed today as well; a small buffer covers hand-offs
            bought_now = sum(int(o[2]) for o in orders if o[0] == "BUY_ANIMAL")
            unplaced_now = max(0, (n_cows + n_sheep) - count_animals(W, "COW") - count_animals(W, "SHEEP"))
            need_today += unplaced_now + bought_now + FEED_BUFFER
        short = 0
        if hour <= 20:
            short = need_today - carried - wheat_now
        else:
            short = n_animals - wheat_now - carried
            if FEED_WHEAT_ALL:
                short += max(0, (n_cows + n_sheep) - n_animals)
        if day >= 28:
            short = min(short, 0)
        if short > 0:
            pr = W.prices.get("WHEAT", 25) + 3
            k = int(min(short, max(0, (cash - 5) // pr)))
            if k > 0:
                orders.append(["BUY_PRODUCT", "WHEAT", k])
                cash -= k * pr

    # ---------------- fertilizer buy-back ----------------
    if FERT_EARLY_SELL and not endgame and len(orders) < 10 and hour == 0 and day < 27:
        need_now = 0
        for (x, y), role in st.roles.items():
            tl = W.tiles[y][x]
            if is_plant(tl) and tl.get("crop") == "STRAWBERRY":
                age = day - int(tl.get("planted_day", day))
                if age in (9, 13) and int(tl.get("fertilized_until_day", -1)) < day:
                    need_now += 1
        have_f = int(shed.get("FERTILIZER", 0)) - sum(int(o[2]) for o in orders if o[0] == "SELL" and o[1] == "FERTILIZER")
        short_f = need_now - max(0, have_f)
        pf = W.prices.get("FERTILIZER", 100) + 3
        if short_f > 0 and pf <= FERT_BUY_MAX:
            k = int(min(short_f, max(0, (cash - cash_reserve) // pf)))
            if k > 0:
                orders.append(["BUY_PRODUCT", "FERTILIZER", k])
                cash -= k * pf

    # ---------------- order-slot priority ----------------
    # The engine processes both players' i-th orders together, unit by unit,
    # then moves to i+1.  A SELL placed at index 0 therefore executes before
    # the opponent's index-1 sell of the same product: put the sells with the
    # most revenue at stake first (premium goods), cheap staples and hires later.
    if SELL_ORDER and W.step > 0:
        sells = [o for o in orders if o[0] == "SELL"]
        rest = [o for o in orders if o[0] != "SELL"]
        def stake(o):
            it, n = o[1], int(o[2])
            p = W.prices.get(it, MARKET_PARAMS.get(it, {}).get("base", 1))
            bonus = SELL_ORDER_PREM if it in PREMIUM else 1.0
            if SELL_STRAW_FIRST and it == "STRAWBERRY":
                bonus *= SELL_STRAW_FIRST       # the common opponents put strawberries at index 0
            return p * n * bonus
        sells.sort(key=stake, reverse=True)
        orders = sells + rest
    # ---------------- day 0 special ordering ----------------
    if W.step == 0:
        orders = [["HIRE"]] * OPEN_HIRES + [
            ["BUY_ANIMAL", "SHEEP", OPEN_SHEEP],
            ["BUY_ANIMAL", "COW", OPEN_COWS],
            ["BUY_SEED", "MELON", OPEN_MELONS],
            ["BUY_SEED", "WHEAT", OPEN_WHEAT_SEEDS],
            ["BUY_PRODUCT", "WHEAT", OPEN_WHEAT_FEED],
        ]
    return orders[:10]


# ----------------------------------------------------------------------------
# main entry
# ----------------------------------------------------------------------------
def must_do_tiles(W):
    """Tiles that must be visited today or value is lost tonight: plants with
    consecutive_unwatered >= 1 (die tonight) and strawberries on a fertilize
    day (age 9/13: the fertilizer doubles the remaining productions)."""
    out = []
    for y, row in enumerate(W.tiles):
        for x, tl in enumerate(row):
            if not is_plant(tl):
                continue
            age = W.day - int(tl.get("planted_day", W.day))
            crop = tl.get("crop")
            cu = int(tl.get("consecutive_unwatered", 0))
            if crop == "STRAWBERRY" and age >= 16:
                continue
            if cu >= 1 and not tl.get("watered_today"):
                out.append((x, y))
            elif crop == "STRAWBERRY" and age in (9, 13) and int(tl.get("fertilized_until_day", -1)) < W.day:
                out.append((x, y))
    return out


def morning_dispatch(st, W):
    """Assign one hand per far cluster of must-do tiles for the day.  The hourly
    greedy re-plan starves far must-dos (nearer jobs of the same tier and the
    on-tile continuation bonus always win), so a dedicated unit is sent once."""
    st.duty = {}
    st.duty_day = W.day
    if W.day >= LAST_DAY or len(W.units) <= 2:
        return
    far = [t for t in must_do_tiles(W) if nearest_shed(t)[1] >= DUTY_FAR_D]
    if not far:
        return
    far.sort(key=lambda t: (-nearest_shed(t)[1], t[1], t[0]))
    clusters = []
    for t in far:
        for c in clusters:
            if dist(t, c[0]) <= DUTY_RADIUS and len(c) < 8:
                c.append(t)
                break
        else:
            clusters.append([t])
    clusters = clusters[:DUTY_MAX]
    # hands only (index >= 1); prefer units already nearest to the cluster
    free = [i for i in range(1, len(W.units))]
    for c in clusters:
        if not free:
            break
        best = min(free, key=lambda i: dist(W.units[i], c[0]))
        free.remove(best)
        st.duty[best] = set(c)


def _drain_step(W, item, step):
    """Town consumption of `item` processed at `step` (engine: each shop instance every 4
    steps, x2 for single-product shops; the town centre 1/day for all but fertilizer)."""
    d = 0
    if step % SHOP_SELL_INT == 0:
        for sh in W.shops:
            prods = SHOPS.get(sh, [])
            if item in prods:
                d += 2 if len(prods) == 1 else 1
    if step % CENTER_SELL_INT == 0 and item != "FERTILIZER":
        d += 1
    return d


def _update_opp_supply(st, W):
    """opp_sold(prev step) = inventory delta + drain - my sales of the previous step."""
    if st.prev_inv is not None and W.step == st.prev_step + 1:
        my = {}
        for o in st.last_orders:
            if isinstance(o, list) and o and o[0] == "SELL":
                my[o[1]] = my.get(o[1], 0) + int(o[2])
        d_prev = st.prev_step // TPD
        for item in MARKET_PARAMS:
            delta = int(W.inv.get(item, MARKET_I0)) - int(st.prev_inv.get(item, MARKET_I0))
            opp = delta + _drain_step(W, item, st.prev_step) - my.get(item, 0)
            if opp > 0:
                dd = st.opp_daily.setdefault(item, {})
                dd[d_prev] = dd.get(d_prev, 0) + opp
    st.prev_inv = dict(W.inv)
    st.prev_step = W.step


def opp_rate(st, item, day, days=None):
    """Opponent's units/day of `item` over the trailing window (days before today)."""
    days = days or MA_DAYS
    dd = st.opp_daily.get(item, {})
    lo = max(0, day - days)
    tot = sum(v for d, v in dd.items() if lo <= d < day)
    n = max(1, min(days, day - lo))
    return tot / float(n)


_ORACLE = None


def _load_oracle():
    global _ORACLE
    if _ORACLE is not None:
        return _ORACLE
    sched = {}
    try:
        if ORACLE_TAPE and os.path.exists(ORACLE_TAPE):
            import json as _json
            ep = _json.load(open(ORACLE_TAPE, encoding="utf-8")); steps = ep["steps"]; pp = int(ORACLE_P)
            for si in range(len(steps) - 1):
                a = steps[si + 1][pp].get("action") or {}
                for o in (a.get("market") or []):
                    if isinstance(o, list) and len(o) >= 3 and o[0] == "SELL":
                        try:
                            sched.setdefault(si, []).append((o[1], int(o[2])))
                        except Exception:
                            pass
    except Exception:
        sched = {}
    _ORACLE = sched
    return sched


def _opp_sells(st, W):
    """Predicted opponent SELL schedule {step: [(item, qty), ...]}.  Oracle in the instrument;
    a lineage/branch prediction from the rival farm's public state is the deployment version."""
    return _load_oracle()


_PRE_ITEMS = ("MILK", "WOOL", "STRAWBERRY", "EGG", "TOMATO", "CARROT", "MELON")


def _preempt_sells(st, W, orders, shed):
    """Market race vs a fixed schedule: the lockstep market prices both players' unit i at the same
    inventory, so the first seller takes the fresh prices.  Sell everything of an item the step
    before the opponent's dump, put contested items at the lowest order index, and hold an item for
    a few steps after their dump (the price is depressed until the shops drain it)."""
    sched = _opp_sells(st, W)
    if not sched:
        return orders
    step = W.step
    nxt = {}; last = {}
    for s2 in range(max(0, step - PREEMPT_HOLD), min(LAST_STEP, step + 1) + 1):
        for it, q in sched.get(s2, []):
            if q < PREEMPT_MIN_Q:
                continue
            if s2 > step:
                if it not in nxt:
                    nxt[it] = (s2, q)
            elif s2 < step:
                last[it] = (s2, q)
    sells = {}
    for o in orders:
        if o[0] == "SELL":
            sells[o[1]] = sells.get(o[1], 0) + int(o[2])
    out = {}
    for it in _PRE_ITEMS + ("FERTILIZER", "WHEAT"):
        have = int(shed.get(it, 0))
        cur = sells.get(it, 0)
        if it in _PRE_ITEMS and it in nxt and nxt[it][0] == step + 1 and have > 0:
            cur = have                       # everything before their dump next step
        elif it in last and it in sells and W.hour < 20 and step - last[it][0] <= PREEMPT_HOLD:
            cur = 0                          # hold after their dump
        if cur > 0:
            out[it] = min(cur, have)
    this = set(it for it, q in sched.get(step, []) if q >= 1)
    ordered = sorted(out.keys(), key=lambda it: (0 if it in this else 1, -out[it] * W.prices.get(it, 0)))
    new = [["SELL", it, out[it]] for it in ordered]
    return new + [o for o in orders if o[0] != "SELL"]


_MLIB = None
_MLIB_B64 = ""      # market-order library of the reference agent (research/build_market_lib.py)


def _load_mlib():
    global _MLIB
    if _MLIB is not None:
        return _MLIB
    lib = {"games": []}
    try:
        import json as _json, zlib as _zlib, base64 as _b64
        if MLIB_PATH and os.path.exists(MLIB_PATH):
            lib = _json.load(open(MLIB_PATH, encoding="utf-8"))
        elif _MLIB_B64:
            lib = _json.loads(_zlib.decompress(_b64.b64decode(_MLIB_B64)).decode())
    except Exception:
        lib = {"games": []}
    _MLIB = lib
    return lib


def _prod_counts(shops):
    c = {}
    for sh in shops:
        for pr in SHOPS.get(sh, []):
            c[pr] = c.get(pr, 0) + 1
    return [c.get(x, 0) for x in ("MILK", "WOOL", "STRAWBERRY", "TOMATO", "CARROT", "EGG")]


def _my_state_vec(W):
    cows = count_animals(W, "COW"); sheep = count_animals(W, "SHEEP"); geese = count_animals(W, "GOOSE")
    return [cows, sheep, geese, count_crop(W, "STRAWBERRY"), count_crop(W, "WHEAT"), count_crop(W, "TOMATO"),
            count_crop(W, "CARROT"), len(G(W.farm, "hands", [])), len(W.unlocked), int(W.money) // 1000]


def _mtape_pick(st, W):
    """Choose the reference game for today: same product-count key (relaxed to nearest key),
    then nearest farm state.  Stores the index in st.mtape_game."""
    lib = _load_mlib(); games = lib.get("games", [])
    if not games:
        st.mtape_game = None
        return
    day = W.day
    k = min(MAX_SHOPS, day // SHOP_UNLOCK_INT)
    key = _prod_counts(list(W.shops)[:k])
    mine = _my_state_vec(W)
    wst = (3, 3, 2, 1, 0.5, 1, 1, 1, 4, 1)
    best = None
    for gi, g in enumerate(games):
        gkey = _prod_counts(g["shops"][:k])
        kd = sum(abs(a - b) for a, b in zip(key, gkey))
        stv = g["st"].get(str(day)) or g["st"].get(day)
        if stv is None:
            continue
        sd = sum(abs(a - b) * w for a, b, w in zip(mine, stv, wst))
        score = kd * MTAPE_KEY_W + sd
        if best is None or score < best[0]:
            best = (score, gi, kd, sd)
    st.mtape_game = best[1] if best else None
    st.mtape_dist = (best[2], best[3]) if best else None


def _mtape_targets(st, W):
    """Macro targets from the reference game's state two days ahead (so the field follows its plan)."""
    lib = _load_mlib(); games = lib.get("games", [])
    gi = getattr(st, "mtape_game", None)
    if gi is None or gi >= len(games):
        return
    g = games[gi]; d2 = min(28, W.day + 2)
    stv = g["st"].get(str(d2)) or g["st"].get(d2)
    if not stv:
        return
    cows, sheep, geese, straw, wheat, tomato, carrot = stv[:7]
    if W.day <= COW_LAST_BUY:
        st.cow_target = max(cows, owned_animals(W, "COW"))
    if W.day <= SHEEP_LAST_BUY:
        st.sheep_target = max(sheep, owned_animals(W, "SHEEP"))
    st.goose_target = max(geese, owned_animals(W, "GOOSE"))
    if W.day <= STRAW_LAST_PLANT:
        st.straw_target = max(straw, count_crop(W, "STRAWBERRY"))
    st.tomato_target = max(tomato, count_crop(W, "TOMATO"))
    st.carrot_target = max(carrot, count_crop(W, "CARROT"))


def _market_tape_orders(st, W, orders):
    lib = _load_mlib(); games = lib.get("games", [])
    gi = getattr(st, "mtape_game", None)
    if gi is None or gi >= len(games):
        return orders
    g = games[gi]
    rec = g["mk"].get(str(W.step)) or g["mk"].get(W.step) or []
    out = []
    shed = W.shed
    for o in rec:
        if not (isinstance(o, list) and o):
            continue
        o = list(o)
        if o[0] == "SELL" and len(o) >= 3:
            have = int(shed.get(o[1], 0))
            if have <= 0:
                continue
            o[2] = min(int(o[2]), have)
        out.append(o)
    # feed safety: never let the herd starve because the reference game had more wheat
    placed = count_animals(W)
    wheat = int(shed.get("WHEAT", 0)) + sum(int(iv.get("WHEAT", 0)) for iv in W.invs)
    if W.hour <= 6 and placed > wheat and not any(o[0] == "BUY_PRODUCT" and o[1] == "WHEAT" for o in out) and W.day < LAST_DAY:
        out.append(["BUY_PRODUCT", "WHEAT", placed - wheat])
    return out[:10]


_TREE = None
_TREE_B64 = ""


def _load_tree():
    global _TREE
    if _TREE is not None:
        return _TREE
    lib = {}
    try:
        import json as _json, zlib as _zlib, base64 as _b64
        if TAPE_TREE_PATH and os.path.exists(TAPE_TREE_PATH):
            lib = _json.load(open(TAPE_TREE_PATH, encoding="utf-8"))
        elif _TREE_B64:
            lib = _json.loads(_zlib.decompress(_b64.b64decode(_TREE_B64)).decode())
    except Exception:
        lib = {}
    _TREE = lib
    return lib


def _shop_sim(a, b):
    if a == b:
        return 1.0
    sa = set(SHOPS.get(a, [])); sb = set(SHOPS.get(b, []))
    return 0.6 * (len(sa & sb) / len(sa | sb)) if (sa | sb) else 0.0


def _town_match(actual, pref):
    """Similarity of a game's shop prefix to the actual draw: later shops weigh more (they decide the schedule)."""
    if not pref:
        return 1.0
    tot = 0.0; wsum = 0.0
    for j, sh in enumerate(pref):
        w = 1.0 + 0.5 * j
        tot += w * (_shop_sim(actual[j], sh) if j < len(actual) else 0.5)
        wsum += w
    return tot / wsum


def _tree_step(W, obs, st):
    """Replay along the action-history tree: at every draw pick the child whose recorded town best matches
    the actual one; the farm stays identical to the followed game's, so the actions remain valid."""
    lib = _load_tree()
    nodes = lib.get("nodes") if lib else None
    if not nodes or getattr(st, "tape_off", False):
        return None
    step = W.step
    k = step // 72
    actual = list(W.shops)
    path = getattr(st, "tree_path", None)
    if path is None:
        path = [lib["root"]]
        force_leaf = os.environ.get("KG_TREE_FORCE_LEAF", "") or TREE_FORCE_LEAF   # instrument: read per game
        if force_leaf and force_leaf in nodes:
            parent = {}
            for nid, nd in nodes.items():
                for c in nd.get("children", []):
                    parent[c] = nid
            chain = [force_leaf]
            while chain[-1] in parent:
                chain.append(parent[chain[-1]])
            path = list(reversed(chain))
    while len(path) < k + 1:
        cur = nodes.get(path[-1])
        kids = cur.get("children", []) if cur else []
        if not kids:
            st.tape_off = True
            st.tree_path = path
            return None
        best = None
        for cid in kids:
            nd = nodes[cid]
            m = max(_town_match(actual, pref) for pref in nd.get("shops", [[]]))
            sc = (m, nd.get("n", 1), nd.get("rew", 0))
            if best is None or sc > best[0]:
                best = (sc, cid)
        if best[0][0] < TAPE_TREE_MIN:
            st.tape_off = True
            st.tree_path = path
            st.tree_miss = getattr(st, "tree_miss", 0) + 1
            return None
        if best[0][0] < 1.0:
            st.tree_miss = getattr(st, "tree_miss", 0) + 1
        path.append(best[1])
    st.tree_path = path
    node = nodes.get(path[k])
    if node is None:
        st.tape_off = True
        return None
    seg = node["seg"]
    idx = step - k * 72
    if idx < 0 or idx >= len(seg) or not isinstance(seg[idx], dict):
        return None
    a = seg[idx]
    n_hands = len(G(W.farm, "hands", []))
    hands = [list(h) if isinstance(h, list) and h else ["PASS"] for h in (a.get("hands") or [])]
    hands = hands[:n_hands] + [["PASS"]] * max(0, n_hands - len(hands))
    farmer = list(a.get("farmer") or ["PASS"])
    acts = [farmer] + hands
    pend = getattr(st, "tape_pending", None)
    if pend is None:
        pend = {}
        st.tape_pending = pend
    for i, act in enumerate(acts):
        if i in pend:                       # retry the intended action after the repair DIG
            acts[i] = pend.pop(i)
            continue
        if act and act[0] in ("PLANT", "BUILD_PASTURE", "BUILD_COOP") and i < len(W.units):
            tl = tile_at(W, tuple(W.units[i]))
            if is_weed(tl):
                pend[i] = act
                acts[i] = ["DIG"]
    market = []
    for o in (a.get("market") or []):
        if isinstance(o, list) and o:
            market.append(list(o))
    st.tape_day = W.day
    return {"farmer": acts[0], "hands": acts[1:], "market": market}


_TAPE_LIB = None
_TAPE_B64 = ""       # filled by dist builds (research/embed_tapes.py); local runs load TAPE_LIB_PATH


def _load_tape_lib():
    global _TAPE_LIB
    if _TAPE_LIB is not None:
        return _TAPE_LIB
    lib = {}
    try:
        import json as _json, zlib as _zlib, base64 as _b64
        if TAPE_LIB_PATH and os.path.exists(TAPE_LIB_PATH):
            lib = _json.load(open(TAPE_LIB_PATH, encoding="utf-8"))
        elif _TAPE_B64:
            lib = _json.loads(_zlib.decompress(_b64.b64decode(_TAPE_B64)).decode())
    except Exception:
        lib = {}
    _TAPE_LIB = lib
    return lib


def _tape_step(W, obs, st):
    """Opening replay: the recorded action of a deterministic top agent for this step, keyed by the
    shop draw seen so far.  None when the draw prefix is not in the library (planner takes over)."""
    lib = _load_tape_lib()
    if not lib:
        return None
    step = W.step
    k = step // 72
    if getattr(st, "tape_off", False):
        return None
    actual = list(W.shops)[:k]
    sfx = "#%d" % k if k > MAX_SHOPS else ""       # draws beyond MAX_SHOPS share the 8-shop prefix
    if TAPE_NEAREST:
        hist = getattr(st, "tape_hist", None)
        if hist is None:
            hist = []
        while len(hist) < min(k, MAX_SHOPS):
            j = len(hist)
            want = actual[j] if j < len(actual) else None
            cands = []
            for kk, v in lib.items():
                if v.get("k") != j + 1:
                    continue
                parts = kk.split("|") if kk else []
                if parts[:-1] != hist:
                    continue
                last = parts[-1]
                if want is not None and last == want:
                    sim = 2.0
                else:
                    a = set(SHOPS.get(last, [])); b = set(SHOPS.get(want, [])) if want else set()
                    sim = (len(a & b) / len(a | b)) if (a | b) else 0.0
                cands.append((sim, v.get("n", 1), last))
            if not cands:
                st.tape_off = True
                st.tape_hist = hist
                return None
            cands.sort(reverse=True)
            hist.append(cands[0][2])
            if cands[0][0] < 2.0:
                st.tape_branch_miss = getattr(st, "tape_branch_miss", 0) + 1
        st.tape_hist = hist
        key = "|".join(hist[:k]) + sfx
    else:
        key = "|".join(actual) + sfx
    node = lib.get(key)
    if node is None:
        st.tape_off = True           # once off the library, stay with the planner (no re-entry)
        return None
    seg = node["acts"]
    idx = step - k * 72
    if idx < 0 or idx >= len(seg) or not isinstance(seg[idx], dict):
        return None
    a = seg[idx]
    n_hands = len(G(W.farm, "hands", []))
    hands = list(a.get("hands", []) or [])
    hands = hands[:n_hands] + [["PASS"]] * max(0, n_hands - len(hands))
    farmer = a.get("farmer") or ["PASS"]
    # weed repair: a PLANT / BUILD on a weed tile must dig first
    def fix(act, pos):
        if isinstance(act, list) and act and act[0] in ("PLANT", "BUILD_PASTURE", "BUILD_COOP") and pos is not None:
            tl = tile_at(W, tuple(pos))
            if is_weed(tl):
                return ["DIG"]
        return act
    farmer = fix(farmer, W.units[0] if W.units else None)
    hands = [fix(h, W.units[i + 1] if i + 1 < len(W.units) else None) for i, h in enumerate(hands)]
    market = [list(o) for o in (a.get("market", []) or []) if isinstance(o, list) and o]
    st.tape_day = W.day
    return {"farmer": list(farmer), "hands": hands, "market": market}


def act(obs):
    W = parse(obs)
    st = get_state(W.player, W.step)
    if TAPE_TREE and W.day < TAPE_OPENING_DAYS:
        ta = _tree_step(W, obs, st)
        if ta is not None:
            st.last_orders = [list(o) for o in ta["market"]]
            return ta
    elif TAPE_OPENING and W.day < TAPE_OPENING_DAYS:
        ta = _tape_step(W, obs, st)
        if ta is not None:
            st.last_orders = [list(o) for o in ta["market"]]
            return ta
    if MARKET_AWARE:
        _update_opp_supply(st, W)
    if st.plan_day != W.day:
        plan_day(st, W)
    else:
        # refresh roles when land unlocked mid-day
        if len(st.roles) != len(unlocked_tiles(W)):
            assign_roles(st, W)
    if DUTY and st.duty_day != W.day and W.hour >= DUTY_HOUR:
        morning_dispatch(st, W)
    jobs = build_jobs(st, W)
    actions = None
    if ROUTE and 1 <= W.day < LAST_DAY:
        try:
            if st.route_day != W.day:
                # plan on the jobs as they will be once the h0 market has stocked
                # feed (FEED actions only exist while the shed has wheat)
                W2 = World()
                W2.__dict__.update(W.__dict__)
                W2.shed = dict(W.shed)
                W2.shed["WHEAT"] = int(W2.shed.get("WHEAT", 0)) + 100
                plan_routes(st, W2, build_jobs(st, W2))
            actions = route_step(st, W, jobs)
        except Exception:
            # safety: a planner fault must not cost the step -- fall back to the
            # hourly greedy for this step (routes are re-planned next day)
            if DEBUG:
                raise
            actions = None
            st.route_day = -1
    if actions is None:
        actions = assign(st, W, jobs)
    orders = market_orders(st, W, actions)
    if MARKET_TAPE and (not TAPE_OPENING or W.day >= TAPE_OPENING_DAYS or getattr(st, "tape_off", False)):
        if getattr(st, "mtape_game", None) is None:
            _mtape_pick(st, W)
        orders = _market_tape_orders(st, W, orders)
    st.last_orders = [list(o) for o in orders]
    farmer = actions[0] if actions else ["PASS"]
    hands = actions[1:len(W.units)]
    return {"farmer": farmer, "hands": hands, "market": orders}


_ENGINE_SYNCED = False


def _sync_engine(config):
    """Balance-change robustness: take the game constants from the engine that is actually
    running (and its configuration) instead of the copies above, so a numeric balance patch
    (prices, curves, costs, intervals) is followed automatically.  Rule changes still need
    code.  Values are identical to the copies on kaggle-environments 1.32.7; any difference
    is reported once on stderr."""
    global _ENGINE_SYNCED, SHED_CAP, SHOP_SELL_INT, CENTER_SELL_INT, SHOP_UNLOCK_INT, MAX_SHOPS, HINGE_GAIN, TPD, MARKET_I0
    if _ENGINE_SYNCED or not ENGINE_SYNC:
        return
    _ENGINE_SYNCED = True
    diffs = []
    try:
        from kaggle_environments.envs.kaggriculture import kaggriculture as _K
        KEYMAP = {
            "MARKET_PARAMS": (("base", "base"), ("T", "T"), ("below_func", "bf"), ("below_target", "bt"),
                              ("above_func", "af"), ("above_target", "at")),
            "CROPS": (("seed", "seed"), ("first_yield_day", "first"), ("max_yield_day", "maxday"),
                      ("interval", "interval"), ("max_yield", "max_yield"), ("ongoing", "ongoing")),
            "ANIMALS": (("cost", "cost"), ("structure", "structure"), ("first_yield_day", "first"),
                        ("interval", "interval"), ("max_held", "max_held"), ("product", "product")),
        }
        for name, mine in (("MARKET_PARAMS", MARKET_PARAMS), ("CROPS", CROPS), ("ANIMALS", ANIMALS), ("SHOPS", SHOPS)):
            theirs = getattr(_K, name, None)
            if isinstance(theirs, dict) and theirs:
                for k, v in theirs.items():
                    if name == "SHOPS":
                        v2 = list(v)
                    else:
                        # translate the engine's key names into the short names used here
                        v2 = dict(mine.get(k, {}))
                        for ek, mk in KEYMAP[name]:
                            if ek in v:
                                v2[mk] = v[ek]
                    if mine.get(k) != v2:
                        diffs.append(f"{name}[{k}]: {mine.get(k)} -> {v2}")
                        mine[k] = v2
        i0 = None
        try:
            i0 = int(next(iter(_K.MARKET_PARAMS.values())).get("I0"))
        except Exception:
            pass
        if i0 is not None and i0 != MARKET_I0:
            diffs.append(f"MARKET_I0: {MARKET_I0} -> {i0}")
            globals()["MARKET_I0"] = i0
        for name, attr in (("HINGE_GAIN", "HINGE_GAIN"), ("MAX_SHOPS", "MAX_SHOP_INSTANCES")):
            v = getattr(_K, attr, None)
            if v is not None and v != globals()[name]:
                diffs.append(f"{name}: {globals()[name]} -> {v}")
                globals()[name] = v
        lp = getattr(_K, "LAND_PRICES", None)
        if isinstance(lp, list) and lp and lp != LAND_PRICES:
            diffs.append(f"LAND_PRICES: {LAND_PRICES} -> {lp}")
            LAND_PRICES[:] = lp
    except Exception as e:          # engine not importable here: keep the copies
        diffs.append(f"engine import failed: {e!r}")
    try:
        if config is not None:
            for key, name in (("shedCapacity", "SHED_CAP"), ("townShopSellInterval", "SHOP_SELL_INT"),
                              ("townCenterSellInterval", "CENTER_SELL_INT"), ("townShopUnlockInterval", "SHOP_UNLOCK_INT"),
                              ("turnsPerDay", "TPD")):
                v = G(config, key, None)
                if v is not None and int(v) != globals()[name]:
                    diffs.append(f"{name}: {globals()[name]} -> {int(v)}")
                    globals()[name] = int(v)
    except Exception as e:
        diffs.append(f"config read failed: {e!r}")
    if diffs:
        try:
            sys.stderr.write("[engine-sync] " + "; ".join(diffs) + chr(10))
        except Exception:
            pass


def agent(obs, config=None):
    try:
        _sync_engine(config)
        return act(obs)
    except Exception:
        if DEBUG:
            raise
        try:
            farms = G(obs, "farms", [])
            p = int(G(obs, "player", 0))
            hands = G(farms[p], "hands", [])
            return {"farmer": ["PASS"], "hands": [["PASS"] for _ in hands], "market": []}
        except Exception:
            return {"farmer": ["PASS"], "hands": [], "market": []}
