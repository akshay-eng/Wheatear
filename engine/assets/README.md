# Shipped platform assets

What a migration needs to run that is *the vendor's*, not a customer's: field
layouts, published tool catalogues, compiled adapters. All of it is identical
for every user on the same platform versions, so it is built once here and
shipped rather than re-derived on every machine.

    orchestrate/     catalog-snapshot.json   the published tool catalogue
                     adapters/               compiled IR <-> Orchestrate adapters
                     corpora/                the platform's shape, structure only

    copilot-studio/  adapters/               compiled Copilot Studio -> IR adapters
                     corpora/                the platform's shape, structure only

## What is deliberately not here

Samples, examples and enum values from a probe. A corpus in a developer's local
store keeps them -- the translator needs real records to infer a mapping from --
but they carry agent names, instructions and tenant GUIDs, so they are stripped
on the way in here. See `foundry/shipping.py`.

## Refreshing

    agent-liftoff foundry probe copilot-studio --export <unpacked-solution> --offline
    agent-liftoff foundry probe orchestrate
    agent-liftoff foundry corridor copilot-studio orchestrate
    agent-liftoff foundry ship

The first three build into the local store; `ship` strips and copies here.
Adapters are keyed on the platform versions they were compiled against, so a
user on those versions loads them and calls no model at all.
