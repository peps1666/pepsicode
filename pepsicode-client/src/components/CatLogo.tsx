export default function CatLogo({ size = 24 }: { size?: number }) {
  return <img src="./app-icon.svg" width={size} height={size} alt="Pepsicode 小猫" draggable={false} />;
}
