/* Copyright 2018 The Chromium Authors. All rights reserved.
   Use of this source code is governed by a BSD-style license that can be
   found in the LICENSE file.
*/
'use strict';
tr.exportTo('cp', () => {
  class ReportTemplateRequest extends cp.RequestBase {
    constructor(options) {
      super(options);
      this.method_ = 'POST';
      this.body_ = new FormData();
      this.body_.set('template', JSON.stringify({
        url: options.url,
        statistics: options.statistics,
        rows: options.rows,
      }));
      this.body_.set('name', options.name);
      this.body_.set('owners', options.owners.join(','));
      this.body_.set('id', options.id);
    }

    get url_() {
      return ReportTemplateRequest.URL;
    }
  }
  ReportTemplateRequest.URL = '/api/report/template';
  return {ReportTemplateRequest};
});
