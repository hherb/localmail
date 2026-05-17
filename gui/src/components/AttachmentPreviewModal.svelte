<script lang="ts">
  let { sha256, filename, onClose } = $props<{
    sha256: string | null;
    filename: string | null;
    onClose: () => void;
  }>();
</script>
