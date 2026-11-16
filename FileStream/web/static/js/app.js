"use strict";
// Remember the source folder locally; only restore a same-origin public folder URL.
document.querySelectorAll("[data-folder-file]").forEach((a) =>
  a.addEventListener("click", () => {
    try {
      sessionStorage.setItem(
        "filestream-folder",
        JSON.stringify({
          file: new URL(a.href).pathname,
          folder: location.pathname,
        }),
      );
    } catch (_) {}
  }),
);
if (location.pathname.startsWith("/gen/")) {
  try {
    const entry = JSON.parse(sessionStorage.getItem("filestream-folder"));
    if (
      entry &&
      entry.file === location.pathname &&
      /^\/gfolder\/[A-Za-z0-9_-]+$/.test(entry.folder)
    ) {
      const a = document.createElement("a");
      a.href = entry.folder;
      a.textContent = "← Back to Folder";
      document.getElementById("main").prepend(a);
    }
  } catch (_) {}
}

(() => {
  const toast = document.getElementById('toast');
  let timer;
  window.filestreamToast = (message) => {
    clearTimeout(timer);
    toast.textContent = message;
    toast.hidden = false;
    timer = setTimeout(() => { toast.hidden = true; }, 4500);
  };
  async function share(url, copy = false) {
    const publicUrl = new URL(url, location.href).href;
    try {
      if (!copy && navigator.share) {
        await navigator.share({ title: document.title, url: publicUrl });
        window.filestreamToast('Sharing complete.');
      } else {
        await navigator.clipboard.writeText(publicUrl);
        window.filestreamToast('Public link copied.');
      }
    } catch (error) {
      window.filestreamToast(error.name === 'AbortError' ? 'Sharing cancelled.' : 'Could not share automatically. Select and copy this public link.');
      if (error.name !== 'AbortError') {
        clearTimeout(timer);
        const input = document.createElement('input');
        input.readOnly = true; input.value = publicUrl;
        input.setAttribute('aria-label', 'Public share link');
        toast.append(input); input.focus(); input.select();
      }
    }
  }
  document.querySelectorAll('[data-share-url]').forEach(button => button.addEventListener('click', () => share(button.dataset.shareUrl)));
  document.querySelectorAll('[data-go-back]').forEach(button => button.addEventListener('click', () => {
    if (history.length > 1) history.back(); else location.assign('/');
  }));
  const node = document.getElementById('folder-config');
  if (!node) return;
  const files = JSON.parse(node.textContent);
  const grid = document.getElementById('file-grid');
  const cards = [...grid.querySelectorAll('[data-file-index]')];
  const search = document.getElementById('file-search');
  const sort = document.getElementById('file-sort');
  const dialog = document.getElementById('file-details');
  let filter = 'all', selected, opener;
  const type = file => {
    const ext = file.name.split('.').pop().toLowerCase();
    if (['video','audio'].includes(file.kind)) return file.kind;
    if ((file.mime || '').startsWith('image/') || /^(png|jpg|jpeg|gif|webp|avif|svg|heic|bmp|tiff)$/.test(ext)) return 'images';
    if (/^(zip|rar|7z|tar|gz|bz2|xz)$/.test(ext)) return 'archive';
    if (/^(pdf|txt|md|docx?|xlsx?|pptx?|csv|epub|mobi|odt|rtf)$/.test(ext) || (file.mime || '').startsWith('text/')) return 'documents';
    if ((file.category || '').toLowerCase() === 'books') return 'documents';
    return 'other';
  };
  const bytes = file => typeof file.size_bytes === 'number' && Number.isFinite(file.size_bytes) && file.size_bytes >= 0 ? file.size_bytes : null;
  cards.forEach((card, i) => {
    card.dataset.type = type(files[i]);
    card.querySelector('.file-format').textContent = files[i].name.includes('.') ? files[i].name.split('.').pop().toUpperCase() : 'FILE';
    // Reuse the locally authored SVG geometry for matching file types.
    const icons = {images:'<rect x="3" y="3" width="18" height="18" rx="3"/><path d="m3 17 6-6 5 5 3-3 4 4"/>', archive:'<path d="M4 8h16v13H4ZM3 3h18v5H3ZM10 12h4"/>'};
    if (icons[card.dataset.type]) card.querySelector('.file-icon svg').innerHTML = icons[card.dataset.type];
    card.querySelector('a').addEventListener('click', event => {
      if (event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
      event.preventDefault();
      selected = files[i]; opener = event.currentTarget;
      document.getElementById('detail-name').textContent = selected.name;
      const metadata = document.getElementById('detail-metadata');
      metadata.replaceChildren();
      for (const [label,value] of [['Size',selected.size],['Type',selected.mime || type(selected)],['Category',selected.category],['Uploader',selected.uploader]]) {
        if (!value) continue;
        const dt = document.createElement('dt'), dd = document.createElement('dd');
        dt.textContent = label; dd.textContent = value; metadata.append(dt, dd);
      }
      const play = document.getElementById('detail-play');
      play.hidden = !selected.playable; play.href = selected.share_url;
      const download = document.getElementById('detail-download'); download.href = selected.share_url;
      // Preserve same-origin return navigation when entering through the drawer.
      for (const link of [play, download]) link.onclick = () => {
        try { sessionStorage.setItem('filestream-folder', JSON.stringify({file:new URL(selected.share_url,location.href).pathname,folder:location.pathname})); } catch (_) {}
      };
      document.getElementById('detail-telegram').href = selected.telegram_url;
      dialog.showModal(); document.getElementById('detail-name').focus();
    });
  });
  function update() {
    const ordered = cards.map((card,i) => ({card,i,file:files[i]}));
    ordered.sort((a,b) => {
      if (sort.value === 'az' || sort.value === 'za') return (sort.value === 'az' ? 1 : -1) * a.file.name.localeCompare(b.file.name, undefined, {sensitivity:'base'}) || a.i-b.i;
      if (sort.value === 'large' || sort.value === 'small') {
        const x=bytes(a.file), y=bytes(b.file);
        if (x === null || y === null) return x === y ? a.i-b.i : x === null ? 1 : -1;
        return (sort.value === 'large' ? y-x : x-y) || a.i-b.i;
      }
      return a.i-b.i;
    });
    let count=0;
    for (const {card,file} of ordered) {
      card.hidden = !file.name.toLowerCase().includes(search.value.trim().toLowerCase()) || (filter !== 'all' && type(file) !== filter);
      if (!card.hidden) count++;
      grid.append(card);
    }
    document.getElementById('result-count').textContent = `${count} of ${files.length} files`;
    document.getElementById('empty-results').hidden = count !== 0 || files.length === 0;
  }
  search.addEventListener('input', update); sort.addEventListener('change', update);
  document.getElementById('clear-search').onclick = () => {search.value='';update();search.focus();};
  document.querySelectorAll('[data-filter]').forEach(button => button.onclick = () => {
    filter=button.dataset.filter;
    document.querySelectorAll('[data-filter]').forEach(b => b.setAttribute('aria-pressed',String(b === button)));
    update();
  });
  document.getElementById('reset-files').onclick = () => {search.value='';document.querySelector('[data-filter="all"]').click();search.focus();};
  document.getElementById('close-details').onclick = () => dialog.close();
  dialog.addEventListener('click', event => {
    const box=dialog.getBoundingClientRect();
    if (event.target === dialog && (event.clientX < box.left || event.clientX > box.right || event.clientY < box.top || event.clientY > box.bottom)) dialog.close();
  });
  dialog.addEventListener('close', () => opener?.focus());
  document.getElementById('detail-share').onclick = () => share(selected.share_url);
  document.getElementById('detail-copy').onclick = () => share(selected.share_url,true);
})();
