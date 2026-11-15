// SPDX-License-Identifier: Elastic-2.0
// Copyright (c) 2026 ClaymoreLab

/** Fixed vector lettering reconstructed in the original wordmark's angular style. */
export function QJinWordmark({
  className,
  decorative = false,
  ...props
}: {
  className?: string;
  decorative?: boolean;
  "data-reveal"?: string;
}) {
  return (
    <svg
      xmlns="http://www.w3.org/2000/svg"
      viewBox="15 25 700 315"
      preserveAspectRatio="xMinYMid meet"
      role={decorative ? undefined : "img"}
      aria-label={decorative ? undefined : "Q 金"}
      aria-hidden={decorative ? true : undefined}
      className={className}
      fill="#fafafa"
      {...props}
    >
      <path
        fillRule="evenodd"
        d="M75 45H223L276 98V245L223 298H75L20 245V98ZM108 108H188L212 132V212L188 236H108L84 212V132Z"
      />
      <path d="M167 197H218L303 333L235 303Z" />
      <path d="M510 30H546L699 148L650 173L528 78L408 174L363 151Z" />
      <path d="M446 148H610L638 182H418Z" />
      <path d="M399 207H655L681 241H376Z" />
      <path d="M504 166H552V306H504Z" />
      <path d="M390 253H433L457 284H411Z" />
      <path d="M600 253H646L617 284H573Z" />
      <path d="M375 291H679L699 326H354Z" />
    </svg>
  );
}
