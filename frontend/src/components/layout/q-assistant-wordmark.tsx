// SPDX-License-Identifier: Elastic-2.0
// Copyright (c) 2026 ClaymoreLab
import { useId, type CSSProperties } from "react";

// Fixed lettering in the launcher's existing bold, angular style.
const LETTERING = [
  "M9 2H48L61 15V48L48 61H9L0 50V14ZM18 15L13 21V42L19 48H39L47 41V22L40 15Z",
  "M34 39H47L67 66L48 61Z",
  "M85 3H119L124 8V60H115V12H94V60H85Z",
  "M93 24H117V32H93ZM93 42H117V50H93ZM81 58H127L131 66H78Z",
  "M131 19H168L164 59L158 67H142L139 58H154L158 28H129Z",
  "M142 2H151C149 31 141 53 125 68L120 59C134 45 139 23 142 2Z",
  "M186 8L244 1L250 9L190 17Z",
  "M185 26H254L258 35H183Z",
  "M180 44H260L263 53H177Z",
  "M219 14H229V60L222 68H207L203 59H219Z",
].join("");

export function QAssistantWordmark({
  className,
  style,
}: {
  className?: string;
  style?: CSSProperties;
}) {
  const id = useId();
  const lettersId = `${id}-letters`;
  const faceId = `${id}-face`;
  const panelId = `${id}-panel`;

  return (
    <svg
      xmlns="http://www.w3.org/2000/svg"
      viewBox="0 0 288 105"
      className={className}
      style={style}
      aria-hidden="true"
      focusable="false"
    >
      <defs>
        <linearGradient id={panelId} x2="0" y2="1">
          <stop stopColor="#232930" />
          <stop offset="1" stopColor="#151b22" />
        </linearGradient>
        <linearGradient id={faceId} x2="0" y2="1">
          <stop stopColor="#ffffff" />
          <stop offset="1" stopColor="#e8eaf0" />
        </linearGradient>
        <path id={lettersId} d={LETTERING} fillRule="evenodd" />
      </defs>
      <rect
        x="1.5" y="1.5" width="285" height="102" rx="10"
        fill={`url(#${panelId})`} stroke="rgb(232 234 240 / 0.14)"
      />
      <path d="M12 4H276M5 17V87M283 17V87" stroke="rgb(232 234 240 / 0.08)" />
      <g transform="translate(12 17)">
        <use href={`#${lettersId}`} transform="translate(0 5)" fill="#070a10" />
        <use href={`#${lettersId}`} transform="translate(0 2.5)" fill="#8a929e" />
        <use href={`#${lettersId}`} fill={`url(#${faceId})`} />
      </g>
    </svg>
  );
}
