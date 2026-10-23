# TypeScript

The package is `convergent-sdk`, for Node.js. Its docs are in
`typescript/docs/` of the public repository, next to the shared `docs/` folder.

## Find the installed version

```bash
npm ls convergent-sdk @opentelemetry/api
```

Use the project's own tool when it has one, such as `pnpm ls` or `yarn why`.
The version picks the docs tag: `typescript-v<version>`.

If the project does not have the package, add it with the project's package
manager, such as `npm install convergent-sdk @opentelemetry/api` or
`pnpm add convergent-sdk @opentelemetry/api`. Keep the project's
`@opentelemetry/api` version when the SDK accepts it. Otherwise report the
mismatch before changing it.

## Language docs

`llms.txt` lists the TypeScript docs under `typescript/docs/`. Read
`typescript/docs/opentelemetry.md` before you touch an existing OpenTelemetry
setup, a Mastra agent, or an application-owned provider.
