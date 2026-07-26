(() => {
  const dialog = document.querySelector("[data-reply-dialog]");
  if (!dialog) return;

  const draft = dialog.querySelector("[data-reply-draft-text]");
  const count = dialog.querySelector("[data-reply-count]");
  const model = dialog.querySelector("[data-reply-model]");
  const error = dialog.querySelector("[data-reply-error]");
  const openReply = dialog.querySelector("[data-open-reply]");
  const copyReply = dialog.querySelector("[data-copy-reply]");
  let activeContentId = "";

  const updateLink = () => {
    count.textContent = `${draft.value.length} / 280`;
    const url = new URL("https://twitter.com/intent/tweet");
    url.searchParams.set("in_reply_to", activeContentId);
    url.searchParams.set("text", draft.value);
    openReply.href = url.toString();
  };

  draft.addEventListener("input", updateLink);
  copyReply.addEventListener("click", async () => {
    await navigator.clipboard.writeText(draft.value);
    copyReply.textContent = "Copied";
    window.setTimeout(() => { copyReply.textContent = "Copy reply"; }, 1200);
  });

  document.querySelectorAll("[data-reply-draft]").forEach((button) => {
    button.addEventListener("click", async () => {
      if (button.dataset.loading === "true") return;
      button.dataset.loading = "true";
      button.disabled = true;
      button.setAttribute("aria-busy", "true");
      error.hidden = true;
      try {
        const form = new FormData();
        form.set("platform", button.dataset.platform || "x");
        form.set("content_id", button.dataset.contentId || "");
        form.set("tone", "thoughtful");
        form.set("language", "auto");
        const response = await fetch("/hot-content/reply-draft", {
          method: "POST",
          body: form,
          headers: { "Accept": "application/json" },
        });
        const payload = await response.json();
        if (!response.ok) throw new Error(payload.detail || "Reply generation failed.");
        activeContentId = payload.content_id;
        draft.value = payload.draft;
        model.textContent = `${payload.model}${payload.cached ? " · cached" : ""}`;
        openReply.hidden = false;
        copyReply.hidden = false;
        openReply.href = payload.reply_url;
        updateLink();
        dialog.showModal();
      } catch (cause) {
        activeContentId = button.dataset.contentId || "";
        draft.value = "";
        model.textContent = "";
        error.textContent = cause instanceof Error ? cause.message : "Reply generation failed.";
        error.hidden = false;
        openReply.hidden = true;
        copyReply.hidden = true;
        updateLink();
        dialog.showModal();
      } finally {
        button.dataset.loading = "false";
        button.disabled = false;
        button.removeAttribute("aria-busy");
      }
    });
  });
})();
