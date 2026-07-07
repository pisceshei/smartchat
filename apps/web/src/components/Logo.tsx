/** SmartChat brand mark — original SVG (chat bubble + bolt), not derived
 *  from any third-party asset. */
export function LogoMark({ size = 28 }: { size?: number }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 32 32"
      xmlns="http://www.w3.org/2000/svg"
      aria-label="SmartChat"
      role="img"
    >
      <rect width="32" height="32" rx="8" fill="#2C5CE6" />
      <path
        d="M9 10.5A3.5 3.5 0 0 1 12.5 7h7A3.5 3.5 0 0 1 23 10.5v5a3.5 3.5 0 0 1-3.5 3.5H15l-4.2 3.6c-.65.56-1.8.1-1.8-.76V10.5Z"
        fill="#fff"
      />
      <path d="M17.8 11.2l-3.3 4h2.2l-.7 2.6 3.3-4h-2.2l.7-2.6Z" fill="#2C5CE6" />
    </svg>
  );
}
