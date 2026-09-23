from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

DEFAULT_SETTINGS = {
    "table_count": "10",
    "timed_rate_single": "4.0",
    "timed_rate_double": "3.5",
    "timed_rate_group": "3.0",
    "package_hour_rate": "150",
    "package_min_table_no": "3",
    "package_max_table_no": "10",
}

AVAILABLE_DISCOUNTS = [100, 95, 90, 85, 80, 75, 70, 60, 50]
DISCOUNT_SCOPE_TABLE_ONLY = "table_only"
DISCOUNT_SCOPE_TABLE_AND_DRINK = "table_and_drink"
DISCOUNT_SCOPE_ALL = "all"
DISCOUNT_PRICING_PERCENTAGE = "percentage"
DISCOUNT_PRICING_PACKAGE_HOURLY = "package_hourly"
DISCOUNT_MODE_ALL = "all"
DISCOUNT_MODE_TIMED = "timed"
DISCOUNT_MODE_PACKAGE = "package"
SUGAR_OPTIONS = ["無糖", "一分糖", "微糖", "半糖", "全糖"]
ICE_OPTIONS = ["熱的", "溫的", "去冰", "微冰", "少冰", "正常"]

EMPLOYEE_COLOR_PALETTE = [
    "#C96A4A",
    "#3D7EA6",
    "#568C65",
    "#A16E2F",
    "#8A67A5",
    "#B84F68",
    "#447C78",
    "#8A6B52",
]

CALENDAR_ITEM_TYPES = {
    "todo": {"label": "待辦", "color": "#B78BC4"},
    "maintenance": {"label": "維修", "color": "#C96A4A"},
    "event": {"label": "活動", "color": "#3D7EA6"},
    "competition": {"label": "比賽", "color": "#568C65"},
    "other": {"label": "其他", "color": "#A9B0BA"},
}

SEED_MENU = {
    "飲料": [
        ("特選紅烏龍", 45), ("可樂", 50), ("蜜香紅茶", 45),
        ("蜂蜜檸檬", 55), ("檸檬紅茶", 55), ("百香紅/綠", 55),
        ("綠茶", 45), ("美式咖啡", 60), ("青茶", 45),
        ("拿鐵咖啡", 70), ("蜜茶", 45), ("阿華田", 65),
        ("奶香紅/綠", 55), ("水", 25), ("鮮奶紅/綠/青", 65),
    ],
    "餐點": [
        ("巧克力厚片", 45), ("奶油厚片", 45), ("蒜香厚片", 45),
        ("花生厚片", 45), ("奶酥厚片", 45), ("水餃", 100),
        ("肉骨茶麵", 70), ("蔥燒牛肉麵", 70), ("肉燥麵", 70),
    ],
}
