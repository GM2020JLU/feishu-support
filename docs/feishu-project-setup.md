# Connect your own Feishu Project space

The local quickstart deliberately has no Project identity. A Feishu bot or
ordinary Feishu IM/Task/Base token is not a Feishu **Project** identity. Use
the officially supported Project client for your tenant and confirm its
current login, API scope, and item-level permissions before enabling reads.
Start from the [official Feishu Project AI and CLI page](https://project.feishu.cn/home/product-ai);
confirm its current installation and authentication instructions for your
organization rather than assuming an ordinary Feishu IM token will work.

1. Create a dedicated Project identity/profile under the control service
   account. Keep its OAuth state outside the repository and coding-worker
   environment. Do not copy the maintainer's profile, database, or test Bug.
2. Discover the real space key, URL slug, Bug type key, required fields,
   workflow states, and allowed operations through the official Project client.
   Do not infer IDs from a URL or copy IDs from historical acceptance notes.
3. In a **private** copy of the configuration, add `project_integration` with
   `write_enabled: false`, an accepted absolute native-client executable path,
   its SHA-256, dedicated profile, canonical host, and only the space/type
   scopes you intend to read. These are deployment-specific values, not
   values to copy from another space. Missing or disabled settings leave
   Project reads unavailable.

   A read-only configuration has this shape; replace **every** illustrative
   value after official discovery. The digest must be the actual accepted
   executable's lowercase SHA-256 (for example, from `sha256sum`):

   ```yaml
   project_integration:
     write_enabled: false
     reader:
       enabled: true
       executable: /absolute/path/to/official-project-client
       sha256: REPLACE_WITH_64_LOWERCASE_HEX_CHARACTERS
       profile: dedicated_profile
       host: project.feishu.cn
     intake_spaces:
       - simple_name: example-space
         project_key: discovered_project_key
         type_keys: [discovered_bug_type]
     search_spaces:
       - simple_name: example-space
         project_key: discovered_project_key
         type_key: discovered_bug_type
         allowed_item_ids: [1234567890]
   ```

   An explicit item-ID list limits searches to those items. Whole-type search
   requires an intentional `allowed_item_ids: null` choice and matching
   permission; do not use it merely to make a demo appear populated.
4. Before scheduling refresh, verify the dedicated identity can read one
   non-sensitive item in that scope and that the control account can use the
   exact pinned executable. A browser login alone does not prove CLI access.
5. For writes, separately review the official operation contract, field
   schema, service identity, per-Bug grants, approval and audit policy.
   `write_enabled` is a deployment opt-in, not a grant. The coding worker must
   never receive the Project profile or full credentials. Unknown write results
   require remote readback and reconciliation before another attempt.

Creating, commenting, changing fields or state, and closing a Bug are distinct
operations. The latter requires accepted repair and verification evidence plus
the configured close approval. None is enabled by the local quickstart. Device
verification, production use and the five coding adapters need their own
acceptance; software-only compilation does not establish device success.
