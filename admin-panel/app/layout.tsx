import "./globals.css";
import { Sprite } from "@/components/Sprite";
import { Providers } from "@/components/Providers";

export const metadata = {
  title: "REFLEXO · Handloom",
  description: "Internal admin panel",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" data-palette="indigo" data-density="comfortable">
      <body>
        <Sprite />
        <Providers>{children}</Providers>
      </body>
    </html>
  );
}
