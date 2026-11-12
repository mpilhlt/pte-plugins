/**
 * Reading view for the archival edition.
 *
 * Registers static/archival-edition-to-html.xsl with the editor's XSL viewer: the transcription as
 * HTML, with one box per page delimited by <pb/>. The view is offered only for documents of the
 * archival edition variant.
 *
 * @import { PluginContext } from '../../../../app/src/modules/plugin-context.js'
 */

const TEI_NS = 'http://www.tei-c.org/ns/1.0';
const XSLT_URL = '/api/plugins/archival-edition/static/archival-edition-to-html.xsl';
const IS_ARCHIVAL_EDITION = '//tei:application[@type="extractor"]//tei:label[@type="variant-id"] = "archival-edition"';

export default class ArchivalHtmlViewExtension extends FrontendExtensionPlugin {
  /** @param {PluginContext} context */
  constructor(context) {
    super(context, { name: 'archival-edition-html-view', deps: ['xsl-viewer'] });
  }

  async start() {
    try {
      const xslDoc = new DOMParser().parseFromString(await this.fetchText(XSLT_URL), 'application/xml');
      const parseError = xslDoc.querySelector('parsererror');
      if (parseError) {
        console.error(`archival-edition: parse error in ${XSLT_URL}:`, parseError.textContent);
        return;
      }
      this.getDependency('xsl-viewer').register({
        label: 'Archival edition (pages)',
        xmlns: TEI_NS,
        xslDoc,
        appliesTo: IS_ARCHIVAL_EDITION
      });
    } catch (error) {
      console.error('archival-edition: failed to register the HTML view:', error);
    }
  }
}
