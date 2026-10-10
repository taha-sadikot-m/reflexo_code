import { readFileSync } from "fs";
import { join } from "path";

export function Sprite() {
  const html = readFileSync(join(process.cwd(), "components/sprite.svg"), "utf8");
  return <div dangerouslySetInnerHTML={{ __html: html }} />;
}
