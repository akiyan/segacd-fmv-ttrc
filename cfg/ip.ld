OUTPUT_ARCH(m68k)

SECTIONS
{
  .text 0xFF0000 :
  {
    *(.text)
  }

  .data :
  {
    . = ALIGN(0x10);
    *(.data)
  }

  ASSERT(. <= 0xFF2000, "Main IP permanent text/data overlaps transient codegen area")

  /* Profile screen assets are needed only before init_main_codegen. The IP
     loader places them at the future generated-code base; codegen then
     deliberately overwrites them for playback. */
  .startup 0xFF2000 :
  {
    *(.startup)
    . = ALIGN(0x10);
  }

  ASSERT(. <= 0xFF4900, "transient startup assets exceed generated-code area")

  /* Runtime state occupies the free high-RAM area after the resident palette
     table. This keeps both the permanent image and the transient startup
     assets out of the generated handler/blitter range. */
  .bss 0xFFD000 (NOLOAD) :
  {
    *(.bss)
  }

  ASSERT(. <= 0xFFFB00, "Main IP runtime state overlaps the stack guard")
}
