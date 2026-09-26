export function DiffViewer({ diff }: { diff: string }) {
  if (!diff.trim()) return <p className="muted">No recorded patch diff.</p>;

  return <pre className="diff-viewer" aria-label="Recorded patch diff"><code>{diff.split("\n").map((line, index) => {
    const tone = line.startsWith("+++") || line.startsWith("---") || line.startsWith("diff ") ? "diff-file"
      : line.startsWith("@@") ? "diff-hunk"
      : line.startsWith("+") ? "diff-add"
      : line.startsWith("-") ? "diff-remove" : "";
    return <span className={`diff-line ${tone}`} key={index}>{line}{"\n"}</span>;
  })}</code></pre>;
}
