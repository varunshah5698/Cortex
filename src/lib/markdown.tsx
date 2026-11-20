import { Fragment, type ReactNode } from "react";

function inline(text: string, keyPrefix: string): ReactNode[] {
  const parts: ReactNode[] = [];
  const pattern = /(\*\*[^*]+\*\*|`[^`]+`|\*[^*\n]+\*)/g;
  let last = 0;
  let m: RegExpExecArray | null;
  let i = 0;
  while ((m = pattern.exec(text))) {
    if (m.index > last) parts.push(text.slice(last, m.index));
    const token = m[0];
    if (token.startsWith("**")) {
      parts.push(<strong key={`${keyPrefix}-${i++}`}>{token.slice(2, -2)}</strong>);
    } else if (token.startsWith("`")) {
      parts.push(<code key={`${keyPrefix}-${i++}`}>{token.slice(1, -1)}</code>);
    } else {
      parts.push(<em key={`${keyPrefix}-${i++}`}>{token.slice(1, -1)}</em>);
    }
    last = m.index + token.length;
  }
  if (last < text.length) parts.push(text.slice(last));
  return parts;
}

export function RichText({ text }: { text: string }) {
  const blocks: ReactNode[] = [];
  const lines = text.split("\n");
  let list: string[] = [];
  let code: string[] | null = null;
  let key = 0;

  const flushList = () => {
    if (!list.length) return;
    blocks.push(
      <ul key={`ul-${key++}`}>
        {list.map((item, i) => (
          <li key={i}>{inline(item, `li-${key}-${i}`)}</li>
        ))}
      </ul>,
    );
    list = [];
  };

  for (const raw of lines) {
    const line = raw.replace(/\s+$/, "");

    if (line.trim().startsWith("```")) {
      if (code) {
        blocks.push(
          <pre key={`pre-${key++}`}>
            <code>{code.join("\n")}</code>
          </pre>,
        );
        code = null;
      } else {
        flushList();
        code = [];
      }
      continue;
    }
    if (code) {
      code.push(raw);
      continue;
    }

    const bullet = line.match(/^\s*[-•]\s+(.*)$/);
    const numbered = line.match(/^\s*\d+[.)]\s+(.*)$/);
    if (bullet || numbered) {
      list.push((bullet ?? numbered)![1]);
      continue;
    }
    flushList();

    if (!line.trim()) continue;

    const heading = line.match(/^(#{1,4})\s+(.*)$/);
    if (heading) {
      const content = inline(heading[2], `h-${key}`);
      blocks.push(
        heading[1].length === 1 ? (
          <h2 key={`h-${key++}`}>{content}</h2>
        ) : (
          <h3 key={`h-${key++}`}>{content}</h3>
        ),
      );
      continue;
    }

    blocks.push(<p key={`p-${key++}`}>{inline(line, `p-${key}`)}</p>);
  }

  if (code) {
    blocks.push(
      <pre key={`pre-${key++}`}>
        <code>{code.join("\n")}</code>
      </pre>,
    );
  }
  flushList();

  return <Fragment>{blocks}</Fragment>;
}
