#!/usr/bin/env nextflow
// aging reanalysis pipeline: Nextflow wiring (D6) around the stage scripts in bin/.
// UNTESTED: this machine has no Nextflow (Java 8, no WSL). The same scripts run locally via run_local.ps1.
// Each process is one stage; every stage writes provenance.json next to its outputs (D9).
nextflow.enable.dsl = 2

params.params_file = "${projectDir}/params.json"
params.accession   = null          // prototype: one accession from the frozen list
params.outdir      = "results"

process DISCOVER {
    publishDir "${params.outdir}/01_discover", mode: 'copy'
    input:  path params_json
    output: path "candidates_*.tsv", emit: frozen
            path "provenance.json"
    script: "python ${projectDir}/bin/discover.py ${params_json} ."
}

process FETCH {
    tag "${accession}"
    publishDir "${params.outdir}/${accession}/02_fetch", mode: 'copy'
    input:  tuple val(accession), path(params_json)
    output: tuple val(accession), path("spectra"), emit: spectra
            path "provenance.json"
    script: "python ${projectDir}/bin/fetch.py ${params_json} ${accession} ."
}

process SEARCH_MM {
    tag "${accession}"
    publishDir "${params.outdir}/${accession}/04_search", mode: 'copy'
    // one dataset per invocation; give it the whole allocation (pyMetaMorpheus 003 Q4)
    input:  tuple val(accession), path(spectra), path(params_json)
    output: path "mm", emit: results
            path "provenance.json"
    script: "python ${projectDir}/bin/search_mm.py ${params_json} ${spectra} ."
}

workflow {
    pj = file(params.params_file)
    DISCOVER(pj)
    // Prototype: the accession is chosen explicitly. Later: read the frozen TSV (keep == yes).
    acc = Channel.of(params.accession)
    FETCH(acc.combine(Channel.of(pj)))
    SEARCH_MM(FETCH.out.spectra.combine(Channel.of(pj)))
}
