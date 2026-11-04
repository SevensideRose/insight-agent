"""Shared chunking types and text-stream helpers.

本模块是分块（chunking）子系统的公共层，被各具体切分策略复用，职责有三：

1. 定义配置与数据结构：ChunkConfig（校验后的用户配置）、ChunkDraft（切分中间产物）、
   Segment（原文位置映射）、Chunker（策略接口）；
2. 提供「文本流」抽象：把解析出的 Block 列表拼成单一字符串，并记录每块的字符区间，
   让下游切分器只需在字符串上做 offset 运算，最后再通过 locate 把 offset 映射回
   heading_path / page；
3. 提供通用后处理：去空白、合并过小片段、递归切分兜底。

设计取舍：
- 配置用 Pydantic（需要校验 + 序列化给前端），中间数据用 dataclass（轻量、无校验开销）；
- Chunker 用 Protocol 而非 ABC：策略实现方无需显式继承，结构化子类型即可，
  降低模块间耦合，也便于测试时用假对象替换；
- split_long_text 对 langchain 做函数内懒加载，避免模块导入时就拉起较重依赖。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field, model_validator

from app.services.parsing import Block

# 支持的切分策略。用 Literal 而非 Enum：
# - 与 JSON 序列化天然兼容（就是字符串）；
# - 前端传参无需额外转换；
# - 类型检查器能对字面量做收窄。
Strategy = Literal["fixed", "recursive", "semantic", "structure", "ai"]


class ChunkConfig(BaseModel):
    """切分配置。所有字段带默认值，前端可只传关心的项。

    校验分两层：
    - Field(ge/le)：单字段的静态范围，Pydantic 自动校验；
    - model_validator(mode="after")：跨字段约束（如 overlap 与 size 的关系），
      必须在所有字段就绪后才能判断。
    """
    strategy: Strategy = "recursive"
    # chunk_size 的范围在 _validate 里按策略再收紧（fixed 更严格）。
    chunk_size: int = Field(default=700, ge=100, le=4096)
    chunk_overlap: int = Field(default=70, ge=0, le=500)
    min_chunk_size: int = Field(default=80, ge=20, le=500)
    # semantic 策略用分位数确定切分阈值（如 90 表示取相似度下降的前 10% 作为边界）。
    semantic_percentile: int = Field(default=90, ge=50, le=99)
    # ai 策略的额外指令，限制长度防止提示词注入/超长。
    ai_instructions: str = Field(default="", max_length=1000)
    # 父子块模式：细粒度子块用于检索，大粒度父块用于召回后扩展上下文。
    parent_child: bool = False
    parent_size: int = Field(default=1600, ge=800, le=4096)

    @model_validator(mode="after")
    def _validate(self) -> "ChunkConfig":
        """跨字段校验。返回 self 是 Pydantic v2 的约定（可链式继续校验）。"""
        # fixed 策略是纯按长度硬切，过大块会破坏可读性，因此上限收到 1024；
        # 其他策略按语义/结构切，允许更大的块。
        if self.strategy == "fixed" and not (100 <= self.chunk_size <= 1024):
            raise ValueError("固定分块的 chunk_size 必须在 100–1024 之间")
        if self.strategy != "fixed" and not (200 <= self.chunk_size <= 4096):
            raise ValueError("该分块策略的 chunk_size 必须在 200–4096 之间")
        # overlap 必须小于 size 的一半，否则切分窗口滑动不足，可能产生大量重复内容甚至死循环。
        # max(1, ...) 防御 chunk_size 极小时 // 2 为 0 导致约束退化为 overlap >= 0（恒真）。
        if self.chunk_overlap >= max(1, self.chunk_size // 2):
            raise ValueError("chunk_overlap 必须小于 chunk_size 的一半")
        # 父块必须严格大于子块，否则父子层级失去意义。
        if self.parent_child and self.parent_size <= self.chunk_size:
            raise ValueError("parent_size 必须大于 chunk_size")
        return self

    def to_public(self) -> dict[str, Any]:
        """导出给前端的字典形式。

        单独封装而非直接调用 model_dump，是为了给对外契约一个稳定入口：
        未来内部字段改名/增减时，可在此处做映射，不破坏 API。
        """
        return self.model_dump()


@dataclass
class ChunkDraft:
    """切分中间产物：尚未落库的切片草稿。

    用 dataclass 而非 Pydantic：
    - 切分是热路径，每个文档可能产生成百上千个 draft，避免 Pydantic 的校验开销；
    - 数据由内部代码构造，可信度高，不需要运行时校验。
    """
    text: str
    # heading_path 记录切片在文档结构中的位置（如 "第3章 > 3.2"），用于引用展示。
    heading_path: str = ""
    # page 可空：纯文本无页码；PDF/Word 解析后才有。
    page: int | None = None
    # meta 用 default_factory 避免可变默认值在实例间共享（dataclass 的经典坑）。
    meta: dict[str, Any] = field(default_factory=dict)
    # level 支持父子块：1 为细粒度子块，2+ 为父块。
    level: int = 1
    # parent_key 是逻辑分组键（非数据库外键），merge_small 用它判断能否跨块合并。
    parent_key: str = ""


class Chunker(Protocol):
    """切分策略的结构化接口。

    用 Protocol 而非 ABC 的理由：
    - 实现方（fixed/recursive/semantic/structure/ai）无需显式继承，避免 import 循环；
    - 测试可用任意满足签名的假对象替换，无需继承体系；
    - 新增策略零侵入。
    """
    name: str

    def chunk(self, blocks: list[Block], config: ChunkConfig) -> list[ChunkDraft]: ...


@dataclass
class Segment:
    """原文中一个 Block 占据的字符区间，用于 offset → 结构信息的反查。"""
    start: int
    end: int
    heading_path: str
    page: int | None


def build_stream(blocks: list[Block]) -> tuple[str, list[Segment]]:
    """把 Block 列表拼成一个字符串，并记录每块的字符区间。

    拼接规则：块之间用 "\\n\\n"（空行）分隔，跳过空块。
    返回 (整串, segments)。segments 与有效块一一对应，顺序一致。

    为什么要把结构信息"压平"成字符串再做切分？
    - 各策略（递归、语义、AI）都只需要在一维文本上找边界，实现简单；
    - 切完后用 offset 反查 Segment，就能恢复 heading_path/page，
      避免每个策略各自维护结构信息、逻辑重复。
    """
    parts: list[str] = []
    segments: list[Segment] = []
    position = 0
    for block in blocks:
        text = (block.text or "").strip()
        # 空块直接跳过：既不进入文本，也不产生 Segment，避免 offset 对不上。
        if not text:
            continue
        # 非首块先追加分隔符，并把 position 推进 2。
        # 注意 start 在追加分隔符「之后」才记录，所以 start 指向正文首字符，
        # 分隔符本身不属于任何 Segment——locate 时 offset 落在分隔符上会归到前一块
        # （见 locate 的兜底逻辑）。
        if parts:
            parts.append("\n\n")
            position += 2
        start = position
        parts.append(text)
        position += len(text)
        segments.append(
            Segment(
                start=start,
                end=position,
                heading_path=block.heading_path or "",
                page=block.page,
            )
        )
    return "".join(parts), segments


def locate(segments: list[Segment], offset: int) -> Segment:
    """根据字符偏移找到所属 Segment。

    线性扫描，O(n)。segments 通常与块数同量级（几十到几百），不值得为此上二分。
    两个兜底：
    - offset 落在块间分隔符上（不属于任何区间）→ 返回最后一个 segment；
    - segments 为空 → 返回零值 Segment，保证调用方无需判空。
    这两个兜底让"边界偏移"不会抛异常，代价是可能把偏移归到错误的结构，
    属于"宁可标错也不崩"的取舍。
    """
    for segment in segments:
        if segment.start <= offset < segment.end:
            return segment
    if segments:
        return segments[-1]
    return Segment(0, 0, "", None)


def normalize_draft_text(text: str) -> str:
    """去掉首尾的空白与换行。用于切分后清理边界，不改变内部内容。"""
    return (text or "").strip("\n \t")


def merge_small(drafts: list[ChunkDraft], min_size: int) -> list[ChunkDraft]:
    """把过小的片段合并到前一个片段，但不跨 parent_key / level。

    规则：
    - 当前 draft 长度 < min_size 才尝试合并；
    - 前一个片段必须同 parent_key、同 level，否则不合并（避免破坏父子结构）；
    - 合并方式：文本用 "\\n" 连接后 strip，meta 做浅合并（后者覆盖前者）。

    注意两个隐含行为：
    1. 第一个 draft 即使小于 min_size 也不会被处理（前面没有可合并对象），
       会原样保留——调用方需接受"可能残留一个极小首块"；
    2. 合并后不再检查前一个片段是否变得过大。即没有 max 上限控制，
       连续多个小片段可能被合并成一个超长片段。若需严格上限，
       应在调用方或此处补充 size 判断。
    """
    if not drafts:
        return []
    result: list[ChunkDraft] = []
    for draft in drafts:
        if (
            result
            and len(draft.text) < min_size
            and result[-1].parent_key == draft.parent_key
            and result[-1].level == draft.level
        ):
            previous = result[-1]
            # 注意是修改 result 中的对象（dataclass 可变），而非替换引用。
            previous.text = (previous.text + "\n" + draft.text).strip()
            # 浅合并：后出现的 meta 覆盖同名键；嵌套 dict 不会深合并。
            previous.meta = {**previous.meta, **draft.meta}
            continue
        result.append(draft)
    return result


def split_long_text(
    text: str,
    size: int,
    overlap: int,
    separators: list[str] | None = None,
) -> list[str]:
    """递归字符切分，作为 structure / semantic 策略的兜底。

    典型用法：当某个结构块或语义块仍然超长时，用此函数二次切分到目标 size。

    - separators 默认优先按段落/换行/中英文句号等自然边界切，
      越靠前优先级越高；都不行才退化到逗号、空格、字符级；
    - keep_separator=True：保留分隔符在片段内，避免句末标点丢失；
    - 结果过滤空串，防止切分器产生纯空白片段。

    langchain_text_splitters 在函数内导入（懒加载）：
    - 避免模块导入时就加载这个较重的可选依赖；
    - 让不使用递归切分的场景（如纯 fixed）完全不触发该依赖。
    """
    from langchain_text_splitters import RecursiveCharacterTextSplitter

    splitter = RecursiveCharacterTextSplitter(
        chunk_size=size,
        chunk_overlap=overlap,
        # 分隔符列表兼顾中英文：中文用 。！？；， 英文用 . , 空格。
        # ". "（点+空格）单列，避免把小数/缩写误当句末。
        separators=separators
        or ["\n\n", "\n", "。", "！", "？", "；", ";", ". ", "，", ",", " "],
        keep_separator=True,
        # 用 len 计字符数（与 ChunkConfig 的 chunk_size 语义一致），
        # 而非默认的 tiktoken 计数，避免额外依赖且与字符预算对齐。
        length_function=len,
    )
    return [part for part in splitter.split_text(text) if part.strip()]