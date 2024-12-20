import { SourceIcon } from "./SourceIcon";

export function WebResultIcon({ url }: { url: string }) {
  const hostname = new URL(url).hostname;
  return hostname == "https://docs.bsmart.dev" ? (
    <img
      className="my-0 py-0"
      src={`https://www.google.com/s2/favicons?domain=${hostname}`}
      alt="favicon"
      height={18}
      width={18}
    />
  ) : (
    <SourceIcon sourceType="web" iconSize={18} />
  );
}
