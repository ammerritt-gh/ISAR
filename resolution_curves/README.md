# Committed resolution curves: SP8 BL43LXU

> **Status:** live
> **Authority:** canonical for the committed copies of the BL43LXU resolution curves (AGENTS.md hard rule 10, its one exception)

`SP8 BL43LXU/` holds verbatim copies of the measured SARF resolution curves for the le Tacon
CsV3Sb5 beamtime at SPring-8 BL43LXU: the 28 analyzers of the card (`a03` to `a41`), all at the
`s50_40_40` slit (40 x 40). Each curve has its `.gpi` sidecar. The `.data` and `.lines` files are not
copied: the reader never reads them.

- **Source.** `real_data/leTacon_SP8_CsV3Sb5/rf/` in the main checkout, copied byte for byte on
  2026-10-25 and never edited.
- **Why they are here.** Operator, 2026-10-25: "Resolutions are much safer for the public to read,
  so you can make a copy for the tests." Nothing else measured goes in this folder.
  `tests/docs/test_no_real_data_in_tests.py` checks that it holds only the curves, their sidecars and
  this README.
- **Slit.** Both le Tacon files (302 K and 97 K) use the `s50_40_40` slit. The loader,
  `isar/synth/bl43_curves.py`, reads these files through the card's `resolution_pattern`.
- **Adding a curve.** Copy another SARF file here, and extend the table below.

Verbatim, never edited. The sha256 of each file, as copied:

| File | sha256 |
|---|---|
| `sarf_24Nov10_s50_40_40_a03` | `d82e4287727ea67fe5c831a2feb88afb36556e9bbdbc1a513e927a8e4cf60fa0` |
| `sarf_24Nov10_s50_40_40_a03.gpi` | `b5170df2478c8f41a340cb301e8068057b1a23cb7680ea3e8157173b3a398f65` |
| `sarf_24Nov10_s50_40_40_a04` | `fe3f8a0e58d1bff0249649379aac8a092d6ad7e98dc5e8741a02317b011cddf5` |
| `sarf_24Nov10_s50_40_40_a04.gpi` | `441c07915a1b19f83eb641d77ba9f27f4d02b4840abd2fe16a22a85a79eb6bb4` |
| `sarf_24Nov10_s50_40_40_a05` | `9a92ff7db7bcb575b4a653cff57c2bdeb7c801ed177f1bd945ad5524d7ce43b4` |
| `sarf_24Nov10_s50_40_40_a05.gpi` | `d0839e137f83c858bd2698dbd6190b81b9d1ca14feb94800ae53d097b8f51ab3` |
| `sarf_24Nov10_s50_40_40_a06` | `f30aa533a01e733e9ab8d0a0aa89afab88e2371c4403cca88d16c0c8d4154304` |
| `sarf_24Nov10_s50_40_40_a06.gpi` | `e922957e88163d3c84112d4247c9197b64b1f32db2e4f5fb02d6a56681fcf48a` |
| `sarf_24Nov10_s50_40_40_a07` | `eec2c433be806514d5fcddc037c841a0b7c16af982d683426a054b1d3f294972` |
| `sarf_24Nov10_s50_40_40_a07.gpi` | `a3877e91d1a31f6cfcb835468effb62c005378457781272f2a28eab48534c681` |
| `sarf_24Nov10_s50_40_40_a08` | `aa448fd911e7c22db12987917d6b468d1cd500066865a4942f551e322492a6ee` |
| `sarf_24Nov10_s50_40_40_a08.gpi` | `065ea776b36f579fa31769d28d9cd879576b48c2ff40f45a7527b6d814e43067` |
| `sarf_24Nov10_s50_40_40_a09` | `252602ab3fc0b0198a615fba3706a4c5a11f0b2e7787b48c3724905ab379ca08` |
| `sarf_24Nov10_s50_40_40_a09.gpi` | `8dbb7eaeea196d1cd68adfaaca69772b9a7789da5de222e88bd0cb3e75811946` |
| `sarf_24Nov10_s50_40_40_a14` | `a217c9ff632cb658fd35d60ec764d54771d6b281ff14798b4789ba6f93137a7a` |
| `sarf_24Nov10_s50_40_40_a14.gpi` | `379073fd5eec7fc759e64d2f99fb06fb231ae1edcfb621bf637c6104fc1dae01` |
| `sarf_24Nov10_s50_40_40_a15` | `d9725e5f67401cfae43d0544570327f4898d039b5567d77b583b826c28cf8074` |
| `sarf_24Nov10_s50_40_40_a15.gpi` | `0118ff29de93edd651aa73827a8ac8ad633c4fc3ce21b8b78c435e559876619f` |
| `sarf_24Nov10_s50_40_40_a16` | `3a2c6c5e83210b94c71ab873931501aa290b79f46b50ef19bf6bcddfea3569a8` |
| `sarf_24Nov10_s50_40_40_a16.gpi` | `919f9ad927bebd728814a59914ca4baa83ed73a00898058d65da5e1a69ea1976` |
| `sarf_24Nov10_s50_40_40_a17` | `751c2148ce7de430bdd862137f8555b85d379f0bcc9af73f36c1c9f59aa91c1c` |
| `sarf_24Nov10_s50_40_40_a17.gpi` | `ccce849eb45474bc60f06ec782c46a1537815d3b07e878e1890c77fcfe6a83a5` |
| `sarf_24Nov10_s50_40_40_a18` | `5b5bef27b6defc30395121f053322cf8fee54c93825fb2d8db501796011c4863` |
| `sarf_24Nov10_s50_40_40_a18.gpi` | `69c858bec65e80d03ca9b73854d6ce10c3d7960d2b7d08596397c489ddd49298` |
| `sarf_24Nov10_s50_40_40_a19` | `52ffec36d6934836d096535bf186e7f2eb0cad5b673720b4e2c87bedbdbd78fe` |
| `sarf_24Nov10_s50_40_40_a19.gpi` | `12ce661cdba07b6e0a284184db4fb8bd9111eef96f93821d592799be4cae0576` |
| `sarf_24Nov10_s50_40_40_a20` | `d7ade2324ffb5592932c079429785e83f9778e6e219b06d5700ccff2ec977f96` |
| `sarf_24Nov10_s50_40_40_a20.gpi` | `67fdd7efd0eeae49195a5dfc58a1e399403b1dc0b1d47732705104fb407df24c` |
| `sarf_24Nov10_s50_40_40_a25` | `8de770423650503b70632188fdf7d0d5232573630703b02509cc6e4eb2834772` |
| `sarf_24Nov10_s50_40_40_a25.gpi` | `5a126269093dc5cc4f411f15aaf8acca8655101709e884704eff49fbec9c061c` |
| `sarf_24Nov10_s50_40_40_a26` | `a77591af5f9571aeeaf84b3467a00d69011043e50421af6174ce27701364d769` |
| `sarf_24Nov10_s50_40_40_a26.gpi` | `8367f19f93d3bd5ec8941210dcfcb339e09a3d4aac22a3438ebde80ff2e2c866` |
| `sarf_24Nov10_s50_40_40_a27` | `775a0ccd39a64ed9247bcd22d59d9a25ad8a5bbdfed2c94533e40aec9a28b1fd` |
| `sarf_24Nov10_s50_40_40_a27.gpi` | `83d29c8f606097f0b511b1fb8bf31b81830eaaeb9f252dba17c1dd1bc90cd622` |
| `sarf_24Nov10_s50_40_40_a28` | `ff283cc8ac99f8bdcb20d851c94aa24304f731d913003980b3a41905d448b44f` |
| `sarf_24Nov10_s50_40_40_a28.gpi` | `d660b4a0e43a5c3ecce23c304f5d332255d2778277f8e3a292dcbb77811f4928` |
| `sarf_24Nov10_s50_40_40_a29` | `d25b5a0b3e709362fef33217152163e35465af29c2b9ea95ddebfe2412644fd3` |
| `sarf_24Nov10_s50_40_40_a29.gpi` | `fa737c2a22974fff7f71c650eefb82cc86928cc6bc7e58976a3982df7b242278` |
| `sarf_24Nov10_s50_40_40_a30` | `757a1f9730c725d45b942872090138fe0b13e6827f4b2b356e9520b93deb4f39` |
| `sarf_24Nov10_s50_40_40_a30.gpi` | `e94235c3d556032620fe6ac6f06379f80150454a3df7a541ce5aa17562e876dd` |
| `sarf_24Nov10_s50_40_40_a31` | `4e91866d47d63ec44a9e05606f015180d7880dd08b2e71fef0afe18c0dd6a875` |
| `sarf_24Nov10_s50_40_40_a31.gpi` | `01eb840ee0836cb628f4129985d5cb989af2776d035b88063feac3ea34e147a0` |
| `sarf_24Nov10_s50_40_40_a35` | `8b1699667fb6d8242bd1b39f3b34fc4b72d3331a084790d4b4d2174b2c71cb94` |
| `sarf_24Nov10_s50_40_40_a35.gpi` | `aa74091ec8819f29ebf925165d0c4c002a7f46534e9b85a5a6cade6351945531` |
| `sarf_24Nov10_s50_40_40_a36` | `e31c32f48bd40529bdc3f22f348a79def35e0f512e5f43af832e2c1d3f4d6292` |
| `sarf_24Nov10_s50_40_40_a36.gpi` | `59272dac614a0bcce53993fa2e1815e17ba1d816cd285108280811c1624e08c6` |
| `sarf_24Nov10_s50_40_40_a37` | `bb98b11cef6df2df01000802d4752a164c4f994e339a185eb44b34315233b706` |
| `sarf_24Nov10_s50_40_40_a37.gpi` | `d3557fa846ae6f9a2079944b0bd07a59aa7cb0fa4d53fb23020d38054556ce95` |
| `sarf_24Nov10_s50_40_40_a38` | `c0eac9671df909c4117a6cd6e98a123bfc0ee04d846636caf19ef587e383f470` |
| `sarf_24Nov10_s50_40_40_a38.gpi` | `5dc2104dd4d527eab8887d8da2352ca44b4ad395b6846f5655d71b642f5ba3a0` |
| `sarf_24Nov10_s50_40_40_a39` | `5be9c986977d61bf5edc436e750d01b62eb70742508ce8140f28175da0b71ae8` |
| `sarf_24Nov10_s50_40_40_a39.gpi` | `b0e4b783df98c215805b9d00f0e7586ea00d2da25577f7d2870663ca9a0a564a` |
| `sarf_24Nov10_s50_40_40_a40` | `6b269ac6d3d16ed8bd92d283aa56525c078ffe4e958731a6c8070ffd66029755` |
| `sarf_24Nov10_s50_40_40_a40.gpi` | `befd1ac9335742fe1f68ea09f3862c7c5fb286066970fca5c22099cc6f822204` |
| `sarf_24Nov10_s50_40_40_a41` | `0e0f8030d3b56756a09a2fc882d6134d439b606bd78de579ba6b475c8bb2a32b` |
| `sarf_24Nov10_s50_40_40_a41.gpi` | `7bf857e91119a5aa22335ae14aede7c55764186fccc868758a5de76746591472` |
