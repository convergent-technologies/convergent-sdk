declare const __CONVERGENT_SDK_VERSION__: string;

/** This package's version, set at build time. "0.0.0" when the source runs unbuilt. */
export const version: string =
  typeof __CONVERGENT_SDK_VERSION__ === "string" ? __CONVERGENT_SDK_VERSION__ : "0.0.0";

/** The convergent.sdk.version value, <language>/<release>. */
export const sdkVersion = `typescript/${version}`;

/** The User-Agent on every request the SDK sends, so the server can tell clients apart. */
export const userAgent = `convergent-sdk-typescript/${version}`;
