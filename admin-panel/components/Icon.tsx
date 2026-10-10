"use client";

export function Icon({
  name,
  className = "ic",
}: {
  name: string;
  className?: string;
}) {
  return (
    <svg className={className} aria-hidden="true">
      <use href={`#i-${name}`} />
    </svg>
  );
}
