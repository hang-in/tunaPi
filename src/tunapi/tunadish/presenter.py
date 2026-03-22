from ..progress import ProgressState
from ..runner_bridge import Presenter
from ..transport import RenderedMessage


class TunadishPresenter(Presenter):
    """ProgressState를 Markdown으로 변환."""

    def render_progress(
        self,
        state: ProgressState,
        *,
        elapsed_s: float,
        label: str = "working",
    ) -> RenderedMessage:
        lines = []

        if label:
            lines.append(f"**{label}** ({elapsed_s:.1f}s)")

        if state.actions:
            for act in state.actions:
                status = "✅" if act.completed else "⏳"
                lines.append(f"- {status} {act.action.title}")

        text = "\n".join(lines).strip() or "⏳ 진행 중..."
        return RenderedMessage(text=text)

    def render_final(
        self,
        state: ProgressState,
        *,
        elapsed_s: float,
        status: str,
        answer: str,
    ) -> RenderedMessage:
        if status == "error":
            return RenderedMessage(text="**❌ 오류 발생**")
        if status == "cancelled":
            return RenderedMessage(text="**⚠️ 실행이 취소되었습니다.**")

        return RenderedMessage(text=answer or "*(응답 없음)*")
