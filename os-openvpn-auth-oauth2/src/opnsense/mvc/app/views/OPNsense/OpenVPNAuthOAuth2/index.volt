{#
 # Copyright (c) 2026 SurfHost.nl
 # SPDX-License-Identifier: MIT
 #}

<script>
    $( document ).ready(function() {
        var data_get_map = {'frm_general_settings':"/api/openvpnauthoauth2/settings/get"};
        mapDataToFormUI(data_get_map).done(function(data){
            formatTokenizersUI();
            $('.selectpicker').selectpicker('refresh');
        });

        updateServiceControlUI('openvpnauthoauth2');

        $("#saveAct").click(function(){
            saveFormToEndpoint("/api/openvpnauthoauth2/settings/set", 'frm_general_settings', function(){
                ajaxCall("/api/openvpnauthoauth2/service/reconfigure", {}, function(data,status) {
                    updateServiceControlUI('openvpnauthoauth2');
                });
            });
        });
    });
</script>

<div class="content-box" style="padding-bottom: 1.5em;">
    {{ partial("layout_partials/base_form",['fields':generalForm,'id':'frm_general_settings']) }}
    <div class="col-md-12">
        <hr/>
        <button class="btn btn-primary" id="saveAct" type="button"><b>{{ lang._('Save') }}</b> <i id="saveAct_progress"></i></button>
    </div>
</div>
