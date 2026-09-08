// Minimal, safe markdown renderer for attachment previews.
// Supports: headings, bold/italic/strike, inline code, fenced code blocks,
// links, unordered/ordered lists, blockquotes, paragraphs.

function escapeHtml(s) {
  return s
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function inline(s) {
  let out = escapeHtml(s);
  out = out.replace(/`([^`]+)`/g, "<code>$1</code>");
  out = out.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  out = out.replace(/(^|[^*])\*([^*\n]+)\*/g, "$1<em>$2</em>");
  out = out.replace(/~~([^~]+)~~/g, "<del>$1</del>");
  out = out.replace(
    /\[([^\]]+)\]\((https?:[^)\s]+)\)/g,
    '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>'
  );
  return out;
}

export function renderMarkdown(src) {
  const lines = (src || "").replace(/\r\n?/g, "\n").split("\n");
  const html = [];
  let i = 0;
  let inCode = false;
  let codeLines = [];
  let listType = null;
  let listItems = [];

  const closeList = () => {
    if (listType) {
      html.push(
        `<${listType}>` +
          listItems.map((it) => `<li>${it}</li>`).join("") +
          `</${listType}>`
      );
      listType = null;
      listItems = [];
    }
  };

  while (i < lines.length) {
    const line = lines[i];

    if (/^```/.test(line)) {
      if (inCode) {
        html.push(`<pre><code>${escapeHtml(codeLines.join("\n"))}</code></pre>`);
        codeLines = [];
        inCode = false;
      } else {
        closeList();
        inCode = true;
      }
      i++;
      continue;
    }
    if (inCode) {
      codeLines.push(line);
      i++;
      continue;
    }

    if (/^\s*$/.test(line)) {
      closeList();
      i++;
      continue;
    }

    const heading = /^(#{1,6})\s+(.*)$/.exec(line);
    if (heading) {
      closeList();
      const lvl = heading[1].length;
      html.push(`<h${lvl}>${inline(heading[2])}</h${lvl}>`);
      i++;
      continue;
    }

    const quote = /^>\s?(.*)$/.exec(line);
    if (quote) {
      closeList();
      html.push(`<blockquote>${inline(quote[1])}</blockquote>`);
      i++;
      continue;
    }

    const ul = /^[-*+]\s+(.*)$/.exec(line);
    const ol = /^\d+[.)]\s+(.*)$/.exec(line);
    if (ul || ol) {
      const wanted = ul ? "ul" : "ol";
      if (listType !== wanted) {
        closeList();
        listType = wanted;
      }
      listItems.push(inline((ul || ol)[1]));
      i++;
      continue;
    }

    closeList();
    html.push(`<p>${inline(line)}</p>`);
    i++;
  }
  if (inCode) html.push(`<pre><code>${escapeHtml(codeLines.join("\n"))}</code></pre>`);
  closeList();
  return html.join("\n");
}
