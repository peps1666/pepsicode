export default function BrandLogo({ size = 24 }: { size?: number }) {
  return <img src="./brand-mark.svg" width={size} height={size} alt="PepsiCode 像素终端标识" draggable={false} />;
}
