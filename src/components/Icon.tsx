import type { SVGProps } from "react";

type IconName =
  | "logo"
  | "collapse"
  | "expand"
  | "plus"
  | "search"
  | "globe"
  | "library"
  | "folder"
  | "history"
  | "chevronDown"
  | "more"
  | "link"
  | "download"
  | "sparkles"
  | "image"
  | "bulb"
  | "sliders"
  | "mic"
  | "attach"
  | "logout"
  | "translate"
  | "question"
  | "pie"
  | "brush"
  | "send"
  | "stop"
  | "trash"
  | "eye"
  | "eyeOff"
  | "arrowRight"
  | "arrowLeft"
  | "check"
  | "alert"
  | "cpu"
  | "layers"
  | "shield"
  | "wallet"
  | "route";

export function Icon({
  name,
  size = 18,
  ...rest
}: { name: IconName; size?: number } & SVGProps<SVGSVGElement>) {
  const common = {
    width: size,
    height: size,
    viewBox: "0 0 24 24",
    fill: "none",
    stroke: "currentColor",
    strokeWidth: 1.6,
    strokeLinecap: "round" as const,
    strokeLinejoin: "round" as const,
    ...rest,
  };

  switch (name) {
    case "logo":
      return (
        <svg viewBox="0 0 24 24" width={size} height={size} {...rest}>
          <rect x="3" y="3" width="8" height="8" rx="2.4" fill="currentColor" opacity=".95" />
          <rect x="13" y="3" width="8" height="8" rx="2.4" fill="currentColor" opacity=".75" />
          <rect x="3" y="13" width="8" height="8" rx="2.4" fill="currentColor" opacity=".75" />
          <rect x="13" y="13" width="8" height="8" rx="2.4" fill="currentColor" opacity=".95" />
        </svg>
      );
    case "collapse":
      return (
        <svg {...common}>
          <rect x="3" y="4.5" width="18" height="15" rx="3" />
          <path d="M9.5 4.5v15" />
        </svg>
      );
    case "expand":
      return (
        <svg {...common}>
          <rect x="3" y="4.5" width="18" height="15" rx="3" />
          <path d="M9.5 4.5v15" />
          <path d="M14.5 10.5 17 12l-2.5 1.5" />
        </svg>
      );
    case "plus":
      return (
        <svg {...common}>
          <path d="M12 5v14M5 12h14" />
        </svg>
      );
    case "search":
      return (
        <svg {...common}>
          <circle cx="11" cy="11" r="6.5" />
          <path d="m16 16 4 4" />
        </svg>
      );
    case "globe":
      return (
        <svg {...common}>
          <circle cx="12" cy="12" r="9" />
          <path d="M3 12h18" />
          <path d="M12 3a14 14 0 0 1 0 18a14 14 0 0 1 0-18" />
        </svg>
      );
    case "library":
      return (
        <svg {...common}>
          <path d="M4.5 19.5A2.5 2.5 0 0 1 7 17h13" />
          <path d="M7 2h13v20H7a2.5 2.5 0 0 1-2.5-2.5v-15A2.5 2.5 0 0 1 7 2z" />
          <path d="M11 2v6l2.2-1.6L15.4 8V2" />
        </svg>
      );
    case "folder":
      return (
        <svg {...common}>
          <path d="M3.5 7.5A2 2 0 0 1 5.5 5.5h3.2l2 2.4h7.8a2 2 0 0 1 2 2v7.6a2 2 0 0 1-2 2H5.5a2 2 0 0 1-2-2z" />
        </svg>
      );
    case "history":
      return (
        <svg {...common}>
          <path d="M3.5 12a8.5 8.5 0 1 0 2.8-6.3" />
          <path d="M3.2 4.2v4.6h4.6" />
          <path d="M12 7.8V12l3 1.9" />
        </svg>
      );
    case "chevronDown":
      return (
        <svg {...common}>
          <path d="m6.5 9.5 5.5 5 5.5-5" />
        </svg>
      );
    case "more":
      return (
        <svg {...common} strokeWidth={0} fill="currentColor">
          <circle cx="5" cy="12" r="1.6" />
          <circle cx="12" cy="12" r="1.6" />
          <circle cx="19" cy="12" r="1.6" />
        </svg>
      );
    case "link":
      return (
        <svg {...common}>
          <path d="M10.2 13.8a4.6 4.6 0 0 0 6.5 0l2.5-2.5a4.6 4.6 0 0 0-6.5-6.5L11.5 6" />
          <path d="M13.8 10.2a4.6 4.6 0 0 0-6.5 0l-2.5 2.5a4.6 4.6 0 0 0 6.5 6.5l1.2-1.2" />
        </svg>
      );
    case "download":
      return (
        <svg {...common}>
          <path d="M12 3.5v11" />
          <path d="m7.5 10.5 4.5 4.5 4.5-4.5" />
          <path d="M4.5 20h15" />
        </svg>
      );
    case "sparkles":
      return (
        <svg {...common}>
          <path d="M11 4.2 12.5 8 16.3 9.5 12.5 11 11 14.8 9.5 11 5.7 9.5 9.5 8z" />
          <path d="M17.5 15.2 18.4 17.6 20.8 18.5 18.4 19.4 17.5 21.8 16.6 19.4 14.2 18.5 16.6 17.6z" />
        </svg>
      );
    case "image":
      return (
        <svg {...common}>
          <rect x="3.5" y="3.5" width="17" height="17" rx="3.2" />
          <circle cx="8.8" cy="9" r="1.5" />
          <path d="m20.5 15.5-4.6-4.6L5.5 20.5" />
        </svg>
      );
    case "bulb":
      return (
        <svg {...common}>
          <path d="M9.2 18h5.6" />
          <path d="M10.4 21h3.2" />
          <path d="M12 3a6 6 0 0 1 3.5 10.9c-.6.4-.9 1.1-.9 1.9v.2H9.4v-.2c0-.8-.3-1.5-.9-1.9A6 6 0 0 1 12 3z" />
        </svg>
      );
    case "sliders":
      return (
        <svg {...common}>
          <path d="M4 7.5h6M14 7.5h6" />
          <path d="M4 16.5h10M18 16.5h2" />
          <circle cx="12" cy="7.5" r="2" />
          <circle cx="16" cy="16.5" r="2" />
        </svg>
      );
    case "mic":
      return (
        <svg {...common}>
          <rect x="9" y="2.8" width="6" height="10.4" rx="3" />
          <path d="M5.5 11.5a6.5 6.5 0 0 0 13 0" />
          <path d="M12 18v3.2" />
        </svg>
      );
    case "attach":
      return (
        <svg {...common}>
          <path d="M20.6 11.3 12 19.9a5 5 0 0 1-7.1-7.1l8.7-8.7a3.4 3.4 0 0 1 4.8 4.8l-8.7 8.7a1.8 1.8 0 0 1-2.5-2.5l8-8" />
        </svg>
      );
    case "logout":
      return (
        <svg {...common}>
          <path d="M9.5 20.5H5.8A2.3 2.3 0 0 1 3.5 18.2V5.8a2.3 2.3 0 0 1 2.3-2.3h3.7" />
          <path d="M15.5 16.5 20 12l-4.5-4.5" />
          <path d="M20 12H9.2" />
        </svg>
      );
    case "translate":
      return (
        <svg {...common}>
          <path d="M3 5.5h9.5" />
          <path d="M7.7 3.4v2.1c0 4.3-2.2 7.9-4.9 9.6" />
          <path d="M5.4 9.8c1 2.9 3.4 5 5.4 5.7" />
          <path d="m13.4 20 3.8-10.6L21 20" />
          <path d="M14.8 16.4h5" />
        </svg>
      );
    case "question":
      return (
        <svg {...common}>
          <circle cx="12" cy="12" r="9" />
          <path d="M9.4 9.4a2.8 2.8 0 0 1 5.4 1c0 1.9-2.8 2.5-2.8 4" />
          <circle cx="12" cy="17.4" r=".9" fill="currentColor" stroke="none" />
        </svg>
      );
    case "pie":
      return (
        <svg {...common}>
          <path d="M12 3.2a8.8 8.8 0 1 1-8.8 8.8" />
          <path d="M12 3.2V12H3.2" />
        </svg>
      );
    case "brush":
      return (
        <svg {...common}>
          <path d="M9.3 12.2 17 4.5a2.6 2.6 0 0 1 3.7 3.7l-7.7 7.7" />
          <path d="M7.3 15.1c-1.6 0-2.9 1.3-2.9 2.9 0 1.2-2.4 1.4-1.9 1.9 1 1 2.3 1.9 3.7 1.9 2.1 0 3.8-1.7 3.8-3.8a2.9 2.9 0 0 0-2.7-2.9z" />
        </svg>
      );
    case "send":
      return (
        <svg {...common} strokeWidth={1.9}>
          <path d="M12 19V5" />
          <path d="m6 11 6-6 6 6" />
        </svg>
      );
    case "stop":
      return (
        <svg {...common} strokeWidth={0} fill="currentColor">
          <rect x="8" y="8" width="8" height="8" rx="1.6" />
        </svg>
      );
    case "trash":
      return (
        <svg {...common}>
          <path d="M4.5 7h15" />
          <path d="M9.5 7V5.2A1.2 1.2 0 0 1 10.7 4h2.6a1.2 1.2 0 0 1 1.2 1.2V7" />
          <path d="M6.5 7l.8 12.1A1.9 1.9 0 0 0 9.2 21h5.6a1.9 1.9 0 0 0 1.9-1.9L17.5 7" />
        </svg>
      );
    case "eye":
      return (
        <svg {...common}>
          <path d="M2.5 12S6 5.5 12 5.5S21.5 12 21.5 12S18 18.5 12 18.5S2.5 12 2.5 12z" />
          <circle cx="12" cy="12" r="2.8" />
        </svg>
      );
    case "eyeOff":
      return (
        <svg {...common}>
          <path d="M3 3l18 18" />
          <path d="M10.6 6.1A9.9 9.9 0 0 1 12 6c6 0 9.5 6 9.5 6a17 17 0 0 1-2.7 3.5" />
          <path d="M6.5 7.7A16.6 16.6 0 0 0 2.5 12S6 18 12 18a9.6 9.6 0 0 0 3.8-.8" />
          <path d="M9.9 9.9a3 3 0 0 0 4.2 4.2" />
        </svg>
      );
    case "arrowRight":
      return (
        <svg {...common}>
          <path d="M4 12h15" />
          <path d="m13.5 6.5 5.5 5.5-5.5 5.5" />
        </svg>
      );
    case "arrowLeft":
      return (
        <svg {...common}>
          <path d="M20 12H5" />
          <path d="m10.5 6.5-5.5 5.5 5.5 5.5" />
        </svg>
      );
    case "check":
      return (
        <svg {...common}>
          <path d="m5 12.5 4.5 4.5L19 6" />
        </svg>
      );
    case "alert":
      return (
        <svg {...common}>
          <circle cx="12" cy="12" r="9" />
          <path d="M12 7.5v5.2" />
          <circle cx="12" cy="16.3" r="0.9" fill="currentColor" stroke="none" />
        </svg>
      );
    case "cpu":
      return (
        <svg {...common}>
          <rect x="6" y="6" width="12" height="12" rx="2.4" />
          <rect x="9.6" y="9.6" width="4.8" height="4.8" rx="1" />
          <path d="M9 3v3M15 3v3M9 18v3M15 18v3M3 9h3M3 15h3M18 9h3M18 15h3" />
        </svg>
      );
    case "layers":
      return (
        <svg {...common}>
          <path d="m12 3 8.5 4.6L12 12.2 3.5 7.6z" />
          <path d="m3.5 12.2 8.5 4.6 8.5-4.6" />
          <path d="m3.5 16.6 8.5 4.6 8.5-4.6" />
        </svg>
      );
    case "shield":
      return (
        <svg {...common}>
          <path d="M12 3 5 5.8v5.4c0 4.4 2.9 7.7 7 9.8 4.1-2.1 7-5.4 7-9.8V5.8z" />
          <path d="m9.3 12 1.9 1.9 3.6-3.7" />
        </svg>
      );
    case "wallet":
      return (
        <svg {...common}>
          <rect x="3" y="6" width="18" height="13" rx="3" />
          <path d="M3 10h18" />
          <circle cx="16.5" cy="14.5" r="1.1" fill="currentColor" stroke="none" />
        </svg>
      );
    case "route":
      return (
        <svg {...common}>
          <circle cx="6" cy="6.5" r="2.4" />
          <circle cx="18" cy="17.5" r="2.4" />
          <path d="M8.4 6.5h5.1a3.4 3.4 0 0 1 0 6.8h-3a3.4 3.4 0 0 0 0 6.9h5.1" />
        </svg>
      );
  }
}
