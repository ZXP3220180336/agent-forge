"""
ResultProcessor 单元测试

覆盖：
    head+tail 截断：边界 / 保留首尾 / marker 含原长度 / head_ratio 自定义 / 空内容
    truncate_result：就地截断 + metadata['truncated'] 标记
    normalize_error：None→'' / 空白清理 / 超长截断
"""

from app.domain.ports.tool_gateway import ToolResult
from app.integration.tools.result_processor import ResultProcessor


def test_short_content_unchanged():
    """len ≤ max_length 原样返回。"""
    assert ResultProcessor().truncate("abc", max_length=10) == "abc"


def test_truncate_keeps_head_tail_with_marker():
    """超限 → 保留 head+tail，marker 含原长度。"""
    content = "A" * 100 + "B" * 100  # 200 字符
    result = ResultProcessor().truncate(content, max_length=100)

    assert result.startswith("A" * 70)  # head = int(100 * 0.7) = 70
    assert result.endswith("B" * 30)  # tail = 100 - 70 = 30
    assert "共 200 字符" in result  # marker 含原长度
    assert "已截断" in result


def test_truncate_tail_preserves_key_tail_segment():
    """结尾关键段（如 traceback 尾部）不丢失。"""
    tail_marker = "FINAL_LINE"
    content = "x" * 1000 + tail_marker
    result = ResultProcessor().truncate(content, max_length=100)

    assert tail_marker in result


def test_truncate_custom_head_ratio():
    """自定义 head_ratio。"""
    content = "A" * 80 + "B" * 20
    result = ResultProcessor().truncate(content, max_length=100, head_ratio=0.5)

    assert result.startswith("A" * 50)
    assert result.endswith("B" * 20)  # tail = 100 - 50 = 50，但 B 只有 20


def test_truncate_empty_content():
    """空内容不截断。"""
    assert ResultProcessor().truncate("") == ""


def test_truncate_result_in_place_sets_metadata():
    """truncate_result 就地截断 + metadata['truncated']=True。"""
    processor = ResultProcessor()
    result = ToolResult(success=True, content="A" * 1000)

    processor.truncate_result(result, max_length=100)

    # head + marker + tail：远小于原始，但含 marker 故略超 max_length
    assert len(result.content) < 1000
    assert result.content.startswith("A" * 70)
    assert result.content.endswith("A" * 30)
    assert "已截断" in result.content
    assert result.metadata["truncated"] is True


def test_truncate_result_unchanged_no_metadata():
    """未超限时不设置 truncated 标记。"""
    processor = ResultProcessor()
    result = ToolResult(success=True, content="short")

    processor.truncate_result(result, max_length=100)

    assert result.content == "short"
    assert result.metadata is None


def test_normalize_error_none_to_empty():
    """None → ''。"""
    assert ResultProcessor().normalize_error(None) == ""


def test_normalize_error_strips_blank_lines():
    """去首尾空白、压缩多余空行。"""
    processor = ResultProcessor()
    cleaned = processor.normalize_error("  \n  错误信息A  \n\n  错误信息B  \n")

    assert cleaned == "错误信息A\n错误信息B"


def test_normalize_error_truncates_long_error():
    """超长错误截断（保留头部）。"""
    processor = ResultProcessor(max_error_length=50)
    cleaned = processor.normalize_error("e" * 200)

    assert len(cleaned) <= 50 + len("...（错误过长已截断）")
    assert cleaned.startswith("e" * 50)
    assert "已截断" in cleaned
