import math
from bench import metrics as M


def test_norm_text_strips_markdown():
    assert M.norm_text("# **Hello**   world\n\n![img](a.png) [link](u)") == "Hello world link"


def test_char_sim_identity_and_empty():
    assert M.char_sim("abc", "abc") == 1.0
    assert M.char_sim("", "") == 1.0
    assert M.char_sim("", "abc") == 0.0
    assert 0.5 < M.char_sim("hello world", "hello wold") < 1.0


def test_cer_word_level_for_latin_char_level_for_cjk():
    assert M.cer("the quick brown fox", "the quick brown fox") == 0.0
    assert math.isclose(M.cer("the quick brown cat", "the quick brown fox"), 0.25)
    assert math.isclose(M.cer("今天天气好", "今天天气很好"), 1 / 6)


def test_compat_residual_counts_kangxi():
    assert M.compat_residual("一二三") == 0
    assert M.compat_residual("⼀⼆三") == 2  # 前两个是康熙部首 U+2F00/2F01


def test_headings_and_f1():
    md = "# Title\n\ntext\n\n## Sub section\n\n### Deep ##\n"
    assert M.md_headings(md) == ["Title", "Sub section", "Deep"]
    assert M.heading_f1(["Title", "Sub section"], ["Title", "Sub Section", "Missing"]) == M.prf(2, 0, 1)[2]
    assert M.heading_f1([], []) == 1.0


def test_tables_and_cell_f1():
    md = "| a | b |\n|---|---|\n| 1 | 2 |\n\npara\n\n| x |\n|:-:|\n| y |\n"
    t = M.md_tables(md)
    assert t == [[["a", "b"], ["1", "2"]], [["x"], ["y"]]]
    assert M.cell_f1(t, [[["a", "b"], ["1", "2"]], [["x"], ["y"]]]) == 1.0
    assert 0 < M.cell_f1(t, [[["a", "b"], ["1", "3"]]]) < 1


def test_order_tau():
    paras = ["first paragraph here ok", "second paragraph here ok", "third paragraph here ok"]
    assert M.order_tau("\n\n".join(paras), paras) == 1.0
    assert M.order_tau("\n\n".join(reversed(paras)), paras) == -1.0
    assert M.order_tau("nothing", paras) is None


def test_score_document_keys():
    s = M.score_document("# T\n\nhello world", {"text": "T hello world", "headings": ["T"], "tables": [], "paragraphs": ["hello world"]})
    assert set(s) >= {"char_sim", "cer", "heading_f1", "cell_f1", "compat_residual"}
    assert s["char_sim"] == 1.0 and s["heading_f1"] == 1.0


def test_cjk_inner_spaces_ignored_in_sim_but_counted():
    assert M.norm_text("看 起来 好") == "看起来好"
    assert M.cjk_inner_spaces("看 起来 好 abc 中 文") == 3
    assert M.char_sim("看 起来", "看起来") == 1.0


def test_canon_number_separators():
    c = M._canon_number
    assert c("132,704,932.32") == "132704932.32"
    assert c("1,234") == "1234"            # 单个 , 后接 3 位 = 千分位
    assert c("1.234") == "1.234"           # 单个 . 一律小数点（否则 3.141 会变 3141）
    assert c("1.234.567") == "1234567"     # 重复出现 = 千分位
    assert c("1.234,56") == "1234.56"      # 两种都有：最后出现的是小数点
    assert c("-248,151.42") == "-248151.42"
    assert c("１２．５") == "12.5"          # 全角
    assert c("007") == "7" and c("1,234.50") == "1234.5" and c("-0.00") == "0"


def test_numbers_does_not_merge_across_spaces():
    # 表格行里两个数被空格分开，绝不能并成一个 token
    assert M.numbers("| 1,234.00 | 5,678.00 |") == ["1234", "5678"]
    assert M.numbers("金额 132,704,932.32 与 -248,151.42 及 18.22% 页 1") == \
        ["132704932.32", "-248151.42", "18.22", "1"]


def test_digit_stats_catches_silent_wrong_values():
    # issue #13 的真实样本：一位数字被认错 + 负号丢失
    s = M.digit_stats("营业收入 132,701,932.32 净额 248,151.42",
                      "营业收入 132,704,932.32 净额 -248,151.42")
    assert s["digit_precision"] == 0.0 and s["digit_recall"] == 0.0 and s["digit_f1"] == 0.0
    assert s["digit_n_truth"] == 2 and s["digit_n_pred"] == 2
    ok = M.digit_stats("a 1,234.00 b 9.33", "a 1234 b 9.33")
    assert ok["digit_f1"] == 1.0
    # 多重集：同一个数出现两次要两次都对
    half = M.digit_stats("5 5", "5")
    assert half["digit_recall"] == 1.0 and half["digit_precision"] == 0.5


def test_score_document_omits_digit_keys_without_numbers():
    s = M.score_document("hello world", {"text": "hello world"})
    assert "digit_f1" not in s
    s2 = M.score_document("total 42", {"text": "total 42"})
    assert s2["digit_f1"] == 1.0 and s2["digit_n_truth"] == 1


def test_norm_text_strips_formulas_but_not_currency():
    # 真值解析器把公式剥成一个空格，产物侧必须同口径，否则正确输出 $…$ 反被扣分
    assert M.norm_text(r"设 $x^2 + y^2 = z^2$ 成立") == "设成立"
    assert M.norm_text("前\n\n$$\\frac{a}{b} = 1$$\n\n后") == "前后"
    assert M.norm_text(r"$\alpha$ 与 $\beta$ 都是希腊字母") == "与都是希腊字母"
    # `$` 也是货币符号：不含 \ ^ _ 的一律不当公式，否则中间正文会被整段吃掉
    assert M.norm_text("价格从 $100 涨到 $200") == "价格从 $100 涨到 $200"
    assert M.norm_text("花了 $5 买 {苹果} 和 $10 买梨") == "花了 $5 买 {苹果} 和 $10 买梨"
    assert M.norm_text("公式 $E=mc^2$ 与货币 $99") == "公式与货币 $99"


def test_formula_digits_excluded_from_digit_stats():
    # 公式两侧同剥 → 公式里的数字不进数字指标（真值本来就没有它们）
    assert M.numbers(r"营收 1,234 万，其中 $\sigma_{2024} = 99$") == ["1234"]
