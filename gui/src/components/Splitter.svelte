<script lang="ts">
  interface Props {
    onResize: (deltaX: number) => void;
    disabled?: boolean;
  }
  let { onResize, disabled = false }: Props = $props();

  let dragging: boolean = $state(false);
  let lastX: number = $state(0);
  let pointerId: number | null = $state(null);

  function onPointerDown(e: PointerEvent): void {
    if (disabled) return;
    dragging = true;
    lastX = e.clientX;
    pointerId = e.pointerId;
    window.addEventListener("pointermove", onPointerMove);
    window.addEventListener("pointerup", onPointerUp);
  }

  function onPointerMove(e: PointerEvent): void {
    if (!dragging || e.pointerId !== pointerId) return;
    const dx = e.clientX - lastX;
    lastX = e.clientX;
    onResize(dx);
  }

  function onPointerUp(e: PointerEvent): void {
    if (e.pointerId !== pointerId) return;
    dragging = false;
    pointerId = null;
    window.removeEventListener("pointermove", onPointerMove);
    window.removeEventListener("pointerup", onPointerUp);
  }
</script>

<div
  class="splitter"
  class:dragging
  class:disabled
  role="separator"
  aria-orientation="vertical"
  tabindex="-1"
  onpointerdown={onPointerDown}
></div>

<style>
  .splitter {
    width: 6px;
    cursor: col-resize;
    background: transparent;
    flex: 0 0 auto;
  }
  .splitter:hover,
  .splitter.dragging {
    background: rgba(0, 0, 0, 0.08);
  }
  .splitter.disabled {
    cursor: default;
    pointer-events: none;
  }
</style>
